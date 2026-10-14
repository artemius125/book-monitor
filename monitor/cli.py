import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from .config import Settings
from .store import Store
from .engine import Engine, records_for_job
from .ingest import read_workbook
from .reports import task_report, task_csv, records_csv


def configure_log(root):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(root / "service.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_job(cfg, store, iid, sheet, scope):
    rows = records_for_job(store, iid, sheet)
    if not rows:
        raise SystemExit("Не найдены строки импорта / листа")
    jid = store.create_job(rows, {"scope": scope})
    engine = Engine(cfg, store)
    await engine.start()
    try:
        while True:
            pending = store.query("SELECT COUNT(*) AS n FROM tasks WHERE job_id=? AND state IN ('pending','running','retry')", (jid,))[0]["n"]
            if not pending:
                break
            await asyncio.sleep(1)
        report = {"job_id": jid, "spent_rub": store.cost(jid), "tasks": task_report(store, jid)}
        p = cfg.data_dir / "reports" / f"{jid}.json"
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        p.with_suffix(".csv").write_text(task_csv(report["tasks"]), encoding="utf-8")
        print(json.dumps({"job_id": jid, "spent_rub": report["spent_rub"], "report": str(p.resolve()),
                          "statuses": {str(s): sum((r["result"] or {}).get("status")==s for r in report["tasks"]) for s in (0,1,2)}}, ensure_ascii=False, indent=2))
    finally:
        await engine.close()


async def manual_session(cfg, url):
    from playwright.async_api import async_playwright
    from .fetch import Fetcher, validate_url
    domain = await validate_url(url)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        fetcher = Fetcher(cfg, Store(cfg.data_dir))
        path = fetcher.session_path(domain)
        await fetcher.close()
        context = await browser.new_context(storage_state=str(path) if path.exists() else None)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print("В открытом браузере самостоятельно войдите / решите CAPTCHA. Затем нажмите Enter здесь.")
        await asyncio.to_thread(input)
        state = await context.storage_state()
        # Retain only cookies applicable to this host and localStorage for exactly this host.
        from urllib.parse import urlsplit
        state["cookies"] = [c for c in state["cookies"] if domain == c["domain"].lstrip(".") or domain.endswith("."+c["domain"].lstrip("."))]
        state["origins"] = [o for o in state["origins"] if urlsplit(o["origin"]).hostname == domain]
        path.write_text(json.dumps(state))
        path.chmod(0o600)
        await browser.close()
        print(f"Сессия сохранена для {domain}. В админке нажмите Повторить.")


def main():
    parser = argparse.ArgumentParser(description="Локальный мониторинг книг")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    run = sub.add_parser("run")
    run.add_argument("import_id")
    run.add_argument("--sheet")
    run.add_argument("--scope", choices=["work", "format"], default="work")
    sub.add_parser("balance")
    sub.add_parser("status")
    exp = sub.add_parser("export")
    exp.add_argument("job_id")
    exp.add_argument("--output", type=Path)
    session = sub.add_parser("session")
    session.add_argument("url")
    args = parser.parse_args()
    cfg = Settings()
    cfg.prepare()
    configure_log(cfg.data_dir)
    store = Store(cfg.data_dir)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        # Enforce one owner of SQLite queue (CLI run or web server, never both).
        import fcntl
        lock = open(cfg.data_dir / ".worker.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Другой worker уже работает с этой базой")
        print(f"Админка: http://127.0.0.1:{args.port}   API: http://127.0.0.1:{args.port}/docs")
        uvicorn.run(create_app(cfg), host="127.0.0.1" if not os.getenv("MONITOR_CONTAINER") else "0.0.0.0", port=args.port, log_level="info", access_log=False)
    elif args.command == "import":
        report = read_workbook(args.file.read_bytes(), args.file.name, store)
        path = cfg.data_dir / "reports" / f"{report['import_id']}-records.csv"
        path.write_text(records_csv(store.query("SELECT * FROM records WHERE import_id=? ORDER BY sheet,row_number", (report["import_id"],))), encoding="utf-8")
        print(json.dumps({**report, "report_csv": str(path.resolve())}, ensure_ascii=False, indent=2))
    elif args.command == "run":
        import fcntl
        lock = open(cfg.data_dir / ".worker.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Сервис уже запущен: создайте проверку в админке или остановите сервер")
        asyncio.run(run_job(cfg, store, args.import_id, args.sheet, args.scope))
    elif args.command == "balance":
        from .ai import AI
        async def show():
            ai = AI(cfg, store)
            try:
                print(json.dumps({**await ai.balance(), "spent_24h_rub":store.cost()}, ensure_ascii=False, indent=2))
            finally:
                await ai.close()
        asyncio.run(show())
    elif args.command == "status":
        print(json.dumps(store.query("SELECT job_id,state,COUNT(*) AS count FROM tasks GROUP BY job_id,state"), ensure_ascii=False, indent=2))
    elif args.command == "export":
        content = task_csv(task_report(store, args.job_id))
        path = args.output or cfg.data_dir / "reports" / f"{args.job_id}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(path.resolve())
    elif args.command == "session":
        asyncio.run(manual_session(cfg, args.url))


if __name__ == "__main__":
    main()
