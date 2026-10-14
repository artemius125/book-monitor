import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from .config import Settings
from .store import Store
from .engine import Engine, records_for_job
from .ingest import read_workbook, url_key
from .reports import task_report, task_csv, records_csv
from .ai import AIUnavailable


class JobInput(BaseModel):
    import_id: str | None = None
    sheet: str | None = None
    urls: list[str] = Field(default_factory=list, max_length=10000)
    scope: str = "work"


class BookInput(BaseModel):
    id: str
    title: str
    author: str
    format: str = "ebook"


def create_app(settings=None):
    cfg = settings or Settings()
    cfg.prepare()
    store = Store(cfg.data_dir)
    engine = Engine(cfg, store)

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        yield
        await engine.close()

    app = FastAPI(title="Book Monitor", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.cfg = store, engine, cfg

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver", "[", "::1"):
            return Response("Localhost only", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response("Cross-origin requests are disabled", status_code=403)
        return await call_next(request)

    @app.get("/")
    def home():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "ai_enabled": cfg.ai_enabled, "ai_model": cfg.ai_model, "vision_model": cfg.vision_model,
                "spent_24h_rub": store.cost(), "budget_24h_rub": cfg.ai_daily_budget_rub}

    @app.get("/api/balance")
    async def balance():
        try:
            return {**await engine.ai.balance(), "spent_24h_rub": store.cost()}
        except AIUnavailable as e:
            return {"available": False, "error": str(e)}

    @app.post("/api/imports")
    async def upload(file: UploadFile = File(...)):
        data = await file.read(10_000_001)
        try:
            report = await asyncio.to_thread(read_workbook, data, Path(file.filename or "input.xlsx").name, store)
        except Exception as e:
            raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Не удалось прочитать Excel")
        return report

    @app.get("/api/imports")
    def imports():
        return [{**r, "report": json.loads(r["report"])} for r in store.query("SELECT * FROM imports ORDER BY created DESC")]

    @app.get("/api/imports/{iid}/records")
    def records(iid: str):
        return [{**r, "raw": json.loads(r["raw"]), "prepared": json.loads(r["prepared"])} for r in store.query("SELECT * FROM records WHERE import_id=? ORDER BY sheet,row_number", (iid,))]

    @app.get("/api/imports/{iid}/report.csv")
    def import_csv(iid: str):
        rows = store.query("SELECT * FROM records WHERE import_id=? ORDER BY sheet,row_number", (iid,))
        return Response(records_csv(rows), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=records.csv"})

    @app.get("/api/books")
    def books():
        return store.books()

    @app.post("/api/catalog")
    def catalog(items: list[BookInput]):
        if len(items)>50000 or any(not b.id.strip() or not b.title.strip() or not b.author.strip() for b in items):
            raise HTTPException(400, "Каталог требует id, название и автора; максимум 50000 книг")
        # Authoritative catalog replaces provisional links; records preserve their original proposed ids.
        with store.db() as db:
            db.execute("DELETE FROM books WHERE provisional=1")
            for b in items:
                db.execute("INSERT OR REPLACE INTO books VALUES(?,?,?,?,0,'[]')", (b.id,b.title,b.author,b.format))
        return {"books": len(items), "catalog_kind": "authoritative"}

    @app.post("/api/jobs")
    def create_job(body: JobInput):
        if body.scope not in ("work", "format"):
            raise HTTPException(400, "scope: work или format")
        if body.import_id:
            rows = records_for_job(store, body.import_id, body.sheet)
        else:
            try:
                rows = [{"url": url_key(u)} for u in body.urls]
            except ValueError as e:
                raise HTTPException(400, str(e))
        if not rows:
            raise HTTPException(400, "Список URL пуст")
        return {"job_id": store.create_job(rows, body.model_dump(exclude={"urls"}))}

    @app.get("/api/jobs")
    def jobs():
        rows = store.query("SELECT j.id,j.created,COUNT(t.id) AS total, SUM(t.state='done') AS done,SUM(t.state='needs_attention') AS attention,SUM(t.state IN ('pending','retry','running')) AS active FROM jobs j LEFT JOIN tasks t ON t.job_id=j.id GROUP BY j.id ORDER BY j.created DESC")
        return [{**r, "spent_rub": store.cost(r["id"])} for r in rows]

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        if not store.query("SELECT id FROM jobs WHERE id=?", (jid,)):
            raise HTTPException(404, "Проверка не найдена")
        return {"job_id": jid, "tasks": task_report(store, jid), "spent_rub": store.cost(jid)}

    @app.get("/api/jobs/{jid}/report.csv")
    def csv_report(jid: str, task_id: list[str] = Query(default=[])):
        rows = task_report(store, jid)
        if task_id:
            rows = [r for r in rows if r["id"] in set(task_id)]
            if not rows:
                raise HTTPException(400, "Выбранные результаты не найдены")
        return Response(task_csv(rows), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=pages.csv"})

    @app.post("/api/tasks/{tid}/retry")
    def retry(tid: str):
        rows = store.query("SELECT state FROM tasks WHERE id=?", (tid,))
        if not rows:
            raise HTTPException(404, "Задача не найдена")
        if rows[0]["state"] in ("running", "pending", "retry"):
            raise HTTPException(409, "Задача уже выполняется")
        store.execute("UPDATE tasks SET state='pending',attempts=0,next_at=-1,result=NULL WHERE id=?", (tid,))
        store.event("manual_retry", "Повтор по запросу оператора", tid, "info")
        return {"state": "pending"}

    @app.get("/api/events")
    def events(after: int = 0):
        return store.query("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT 200", (after,))

    @app.get("/api/event-history")
    def history(offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=100),
                job_id: str | None = None, level: str | None = None, search: str = ""):
        clauses, args = [], []
        if job_id:
            clauses.append("t.job_id=?")
            args.append(job_id)
        if level:
            if level not in ("info", "warning", "error"):
                raise HTTPException(400, "Неизвестный уровень события")
            clauses.append("e.level=?")
            args.append(level)
        if search:
            clauses.append("(e.message LIKE ? OR e.code LIKE ? OR t.url LIKE ?)")
            args.extend(["%"+search+"%"]*3)
        join = " FROM events e LEFT JOIN tasks t ON e.task_id=t.id"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = store.query("SELECT COUNT(*) AS n"+join+where,args)[0]["n"]
        items = store.query("SELECT e.*,t.url,t.job_id"+join+where+" ORDER BY e.id DESC LIMIT ? OFFSET ?",[*args,limit,offset])
        return {"items":items,"total":total,"offset":offset,"limit":limit}

    @app.get("/api/artifacts/{name}")
    def artifact(name: str):
        if Path(name).name != name or not name.endswith((".png", ".json", ".txt")):
            raise HTTPException(400, "Недопустимое имя")
        p = cfg.data_dir / "evidence" / name
        if not p.is_file():
            raise HTTPException(404, "Артефакт не найден")
        return FileResponse(p)

    @app.post("/api/tasks/{tid}/diagnose")
    async def diagnose(tid: str):
        rows = store.query("SELECT * FROM tasks WHERE id=?", (tid,))
        if not rows:
            raise HTTPException(404, "Задача не найдена")
        task = rows[0]
        result = json.loads(task["result"] or "{}")
        shot = result.get("screenshot")
        if not shot or Path(shot).name != shot:
            raise HTTPException(400, "Нет снимка защитной страницы")
        try:
            diagnosis = await engine.ai.diagnose_challenge((cfg.data_dir / "evidence" / shot).read_bytes(), task)
        except AIUnavailable as e:
            raise HTTPException(503, str(e))
        result["challenge_diagnosis"] = diagnosis.model_dump()
        store.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), tid))
        store.event("challenge_diagnosis", diagnosis.description, tid, "info")
        return diagnosis

    @app.post("/api/sessions/{domain}")
    async def session(domain: str, file: UploadFile = File(...)):
        if not domain or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in domain.lower()):
            raise HTTPException(400, "Недопустимый домен")
        try:
            data = json.loads(await file.read(1_000_001))
            if not isinstance(data.get("cookies"), list) or not isinstance(data.get("origins", []), list):
                raise ValueError()
            for c in data["cookies"]:
                cd = c["domain"].lstrip(".")
                if not (domain == cd or domain.endswith("."+cd)):
                    raise ValueError()
            for origin in data.get("origins", []):
                if urlsplit(origin["origin"]).hostname != domain:
                    raise ValueError()
            p = engine.fetcher.session_path(domain)
            p.write_text(json.dumps(data))
            p.chmod(0o600)
        except Exception:
            raise HTTPException(400, "Нужен корректный Playwright storage_state только для выбранного домена")
        store.event("session_saved", f"Сохранена сессия для {domain}; повторите нужные проверки", level="info")
        return {"saved": True, "domain": domain}

    return app
