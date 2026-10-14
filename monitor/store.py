import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager

log = logging.getLogger("monitor")


class Store:
    def __init__(self, root):
        self.path = root / "monitor.sqlite3"
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, filename TEXT, created REAL, report TEXT);
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, import_id TEXT, sheet TEXT, row_number INTEGER, raw TEXT, prepared TEXT);
            CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY, title TEXT, author TEXT, format TEXT, provisional INTEGER, source_ids TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL, options TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, job_id TEXT, record_id TEXT, url TEXT, state TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, result TEXT, updated REAL);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(state,next_at);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, level TEXT, task_id TEXT, code TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS ai_calls(id INTEGER PRIMARY KEY, created REAL, job_id TEXT, task_id TEXT, model TEXT, cost REAL, usage TEXT);
            CREATE TABLE IF NOT EXISTS ai_cache(key TEXT PRIMARY KEY, created REAL, response TEXT);
            CREATE TABLE IF NOT EXISTS page_cache(key TEXT PRIMARY KEY, created REAL, response TEXT);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def query(self, sql, args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.db() as db:
            db.execute(sql, args)

    def event(self, code, message, task_id=None, level="warning"):
        self.execute("INSERT INTO events(created,level,task_id,code,message) VALUES(?,?,?,?,?)",
                     (time.time(), level, task_id, code, message))
        getattr(log, level if level in ("info", "warning", "error") else "info")("%s task=%s %s", code, task_id or "-", message)

    def books(self):
        result = self.query("SELECT * FROM books")
        for r in result:
            r["source_ids"] = json.loads(r["source_ids"])
        return result

    def create_job(self, records, options):
        jid = uuid.uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?)", (jid, now, json.dumps(options)))
            for r in records:
                db.execute("INSERT INTO tasks(id,job_id,record_id,url,state,updated) VALUES(?,?,?,?,?,?)",
                           (uuid.uuid4().hex, jid, r.get("id"), r["url"], "pending", now))
        self.event("job_created", f"Создана проверка: {len(records)} URL", level="info")
        return jid

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT * FROM tasks WHERE state IN ('pending','retry') AND next_at<=? ORDER BY updated LIMIT 1", (time.time(),)).fetchone()
            if not r:
                return None
            db.execute("UPDATE tasks SET state='running', attempts=attempts+1,updated=? WHERE id=?", (time.time(), r["id"]))
            out = dict(r)
            out["attempts"] += 1
            return out

    def finish(self, tid, state, result, next_at=0):
        self.execute("UPDATE tasks SET state=?, result=?, next_at=?, updated=? WHERE id=?",
                     (state, json.dumps(result, ensure_ascii=False), next_at, time.time(), tid))

    def cost(self, job_id=None):
        if job_id:
            rows = self.query("SELECT COALESCE(SUM(cost),0) AS total FROM ai_calls WHERE job_id=?", (job_id,))
        else:
            # Rolling 24h avoids dependence on the host timezone.
            rows = self.query("SELECT COALESCE(SUM(cost),0) AS total FROM ai_calls WHERE created>?", (time.time()-86400,))
        return rows[0]["total"]

    def cache_get(self, table, key, ttl):
        assert table in ("ai_cache", "page_cache")
        rows = self.query(f"SELECT response FROM {table} WHERE key=? AND created>?", (key, time.time()-ttl))
        return json.loads(rows[0]["response"]) if rows else None

    def cache_put(self, table, key, response):
        assert table in ("ai_cache", "page_cache")
        self.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?,?)", (key, time.time(), json.dumps(response, ensure_ascii=False)))
