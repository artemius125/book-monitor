import json
import time
from fastapi.testclient import TestClient
from monitor.api import create_app
from monitor.config import Settings


def test_history_can_page_past_200_and_filter_job(tmp_path):
    cfg=Settings(_env_file=None,data_dir=tmp_path,ai_enabled=False)
    app=create_app(cfg)
    store=app.state.store
    jid=store.create_job([{"url":"https://example.com/one"},{"url":"https://example.com/two"}],{})
    tasks=store.query("SELECT * FROM tasks WHERE job_id=?",(jid,))
    for t in tasks:
        store.finish(t["id"],"done",{"status":0,"reason":"test"})
    with store.db() as db:
        db.executemany("INSERT INTO events(created,level,task_id,code,message) VALUES(?,?,?,?,?)",
                       [(time.time(),"warning",tasks[0]["id"],"timeout",f"history-item {i}") for i in range(230)])
    with TestClient(app) as client:
        first=client.get("/api/event-history",params={"search":"history-item","limit":10}).json()
        last=client.get("/api/event-history",params={"search":"history-item","limit":10,"offset":220}).json()
        assert first["total"]==230 and len(last["items"])==10
        assert not set(e["id"] for e in first["items"]) & set(e["id"] for e in last["items"])
        filtered=client.get("/api/event-history",params={"job_id":jid,"level":"warning","search":"history-item"}).json()
        assert filtered["total"]==230
        assert client.get("/api/event-history",params={"job_id":"missing"}).json()["total"]==0
        assert client.get("/api/event-history",params={"limit":1000}).status_code==422
        assert client.get("/api/event-history",params={"level":"invalid"}).status_code==400
        exported=client.get(f"/api/jobs/{jid}/report.csv",params={"task_id":tasks[0]["id"]})
        assert exported.status_code==200 and tasks[0]["url"] in exported.text
        assert tasks[1]["url"] not in exported.text
        assert client.get(f"/api/jobs/{jid}/report.csv",params={"task_id":"foreign"}).status_code==400
