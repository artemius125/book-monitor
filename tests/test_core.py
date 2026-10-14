import asyncio
import io
import json
from pathlib import Path
import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient
from monitor.config import Settings
from monitor.store import Store
from monitor.ingest import norm, author_key, read_workbook
from monitor.matching import decide_match, collection_match
from monitor.extract import html_extract, access_problem, classify
from monitor.fetch import Fetcher, FetchError, validate_url
from monitor.engine import Engine
from monitor.ai import AI, Analysis, AIUnavailable
from monitor.api import create_app
from monitor.reports import safe_cell


@pytest.fixture
def cfg(tmp_path):
    c = Settings(_env_file=None, data_dir=tmp_path, ai_enabled=False, domain_delay=0, browser_enabled=False)
    c.prepare()
    return c


@pytest.fixture
def store(cfg):
    return Store(cfg.data_dir)


def book(title="Тайновидец. Том 13: Мастер невезения", author="Алекс Рудин", id="b1"):
    return {"id":id,"title":title,"author":author,"format":"ebook","provisional":0,"source_ids":[]}


def workbook_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Задание 1"
    ws.append(["Правообладатель", "Автор", "Название", "Тип", "url", "Сайт"])
    ws.append(["Литрес", "Алекс Рудин", "Тайновидец. Том 13", "ebook", "https://example.com/a", "example.com"])
    ws.append(["Литрес", "Рудин Алекс", "Тайновидец. Том 13", "ebook", "https://example.com/a#x", "example.com"])
    ws.append(["Литрес", "Другой Автор", "Тайновидец. Том 13", "ebook", "https://example.com/b", "example.com"])
    s = io.BytesIO()
    wb.save(s)
    return s.getvalue()


def test_import_preserves_source_and_distinguishes_duplicates(store):
    r = read_workbook(workbook_bytes(), "input.xlsx", store)
    assert r["rows"] == 3 and r["provisional_books"] == 2
    assert r["duplicate_url_groups"] == 1 and r["ambiguous_titles"] == 1
    rows = store.query("SELECT * FROM records ORDER BY row_number")
    assert json.loads(rows[0]["raw"])["Автор"] == "Алекс Рудин"
    assert json.loads(rows[1]["raw"])["Автор"] == "Рудин Алекс"
    assert json.loads(rows[0]["prepared"])["same_book_rows"] == [rows[1]["id"]]
    read_workbook(workbook_bytes(), "input.xlsx", store)
    assert len(store.books()) == 2 and len(store.query("SELECT * FROM records")) == 3


def test_match_permuted_author():
    assert decide_match(book()["title"], "Рудин Алекс", [book()])["state"] == "matched"


def test_missing_author_is_ambiguous():
    assert decide_match(book()["title"], "", [book()])["state"] == "ambiguous"


def test_same_title_different_author_not_merged():
    b = [book("Измена", "Анна", "a"),book("Измена", "Ольга", "b")]
    assert decide_match("Измена", "", b)["state"] == "ambiguous"
    assert decide_match("Измена", "Ольга", b)["book"]["id"] == "b"


def test_volume_conflict():
    assert decide_match("Тайновидец. Том 12: Мастер невезения", "Алекс Рудин", [book()])["state"] != "matched"


def test_roman_volume_conflict():
    assert decide_match("Хроники. Книга II", "Алекс Рудин", [book("Хроники. Книга I")])["state"] != "matched"


def test_extraction_ignores_recommendations():
    html = '<article><h1>Тайновидец. Том 13: Мастер невезения</h1><p>Автор: Рудин Алекс</p><p>Описание книги</p><h3>Похожие книги</h3><a href="/download/another.fb2">FB2</a></article>'
    x = html_extract(html,"https://example.com")
    assert x["author"] == "Рудин Алекс" and not x["evidence"]
    assert "Похожие" not in x["text"]


def test_audio_format_and_scope():
    x = html_extract('<article><h1>Тайновидец. Том 13: Мастер невезения</h1><p>Автор: Рудин Алекс</p><audio src="a.mp3"></audio></article>',"https://example.com")
    m = decide_match(x["title"],x["author"],[book()])
    assert classify(x,m,"ebook","work")["status"] == 1
    assert classify(x,m,"ebook","format")["status"] == 0
    assert classify(x,m,"ebook","work")["format_mismatch"]


def test_collection_book_is_found_in_explicit_list():
    x=html_extract('<article><h1>Сборник произведений</h1><p>Автор: Евгений Нетт</p><p>Список книг:</p><p>1. Директива: Выжить!</p><p>6. Директива: Сохранить!</p><p>Формат: FB2</p><a href="/download/1">Скачать книгу</a></article>',"https://example.com")
    m=collection_match(x,[book("Директива: Сохранить!","Евгений Нетт")])
    assert m["state"]=="matched" and m["collection"]
    assert classify(x,m)["status"]==1


def test_loading_shell_is_not_a_book_mismatch():
    assert access_problem('<h1>Загрузка</h1><p>Не удалось загрузить страницу.</p>')[0]=="js_required"


def test_challenge_and_login_detection():
    assert access_problem('<title>Just a moment...</title><form id="challenge-form"></form>')[0] == "captcha"
    assert access_problem('<h1>Вход</h1><input type="password">')[0] == "auth_required"
    assert access_problem('<p>Чтобы читать книгу, зарегистрируйтесь.</p>')[0] == "auth_required"
    assert access_problem('<article>'+"Книга и автор "*150+'</article><div class="g-recaptcha"></div>') is None
    assert access_problem("",429)[0] == "rate_limit"


@pytest.mark.parametrize("url",["file:///etc/passwd","http://127.0.0.1/a","http://169.254.169.254/","http://user:pass@example.com","https://example.com:8000"])
async def test_ssrf_blocked(url):
    with pytest.raises(FetchError):
        await validate_url(url)


async def test_queue_recovery_and_retry(store,cfg):
    jid = store.create_job([{"url":"https://example.com"}],{})
    t = store.claim()
    engine = Engine(cfg,store)
    await engine.handle_error(t,FetchError("timeout","timeout",True))
    row = store.query("SELECT * FROM tasks")[0]
    assert row["state"] == "retry" and json.loads(row["result"])["status"] == 2
    t["attempts"] = cfg.max_attempts
    await engine.handle_error(t,FetchError("timeout","timeout",True))
    assert store.query("SELECT state FROM tasks")[0]["state"] == "needs_attention"
    await engine.close()


async def test_404_is_zero_and_provenance(store,cfg):
    engine = Engine(cfg,store)
    store.create_job([{"url":"https://example.com/404"}],{"scope":"work"})
    t = store.claim()
    async def fake(*args,**kwargs):
        return {"missing":True,"url":args[0],"http_status":404,"method":"http"}
    engine.fetcher.fetch = fake
    result = await engine.process(t)
    assert result["status"] == 0 and result["http_status"] == 404
    await engine.close()


async def test_ai_budget_prevents_paid_post(cfg,store,tmp_path):
    key=tmp_path / "secret"
    key.write_text("test-key")
    cfg.ai_enabled=True
    cfg.ai_key_file=key
    cfg.ai_base_url="https://provider.example/v1"
    cfg.ai_balance_path=""
    cfg.ai_daily_budget_rub=.001
    ai=AI(cfg,store)
    async def forbidden(request):
        pytest.fail("Paid request must not be sent")
    await ai.client.aclose()
    ai.client=httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    with pytest.raises(AIUnavailable,match="Лимит"):
        await ai.call(Analysis,"system","input","j","t")
    await ai.close()


async def test_invalid_ai_json_is_billed_but_not_used(cfg,store,tmp_path):
    key=tmp_path / "secret"
    key.write_text("test-key")
    cfg.ai_enabled=True;cfg.ai_key_file=key;cfg.ai_base_url="https://provider.example/v1";cfg.ai_balance_path=""
    ai=AI(cfg,store)
    await ai.client.aclose()
    async def response(request):
        return httpx.Response(200,json={"usage":{"cost_rub":.2},"choices":[{"message":{"content":"not json"}}]})
    ai.client=httpx.AsyncClient(transport=httpx.MockTransport(response))
    with pytest.raises(AIUnavailable):
        await ai.call(Analysis,"system","input","j","t")
    assert store.cost("j") == .2
    await ai.close()


async def test_concurrent_identical_ai_requests_only_bill_once(cfg,store,tmp_path):
    key=tmp_path / "secret"
    key.write_text("test-key")
    cfg.ai_enabled=True;cfg.ai_key_file=key;cfg.ai_base_url="https://provider.example/v1";cfg.ai_balance_path=""
    ai=AI(cfg,store)
    await ai.client.aclose()
    requests=[]
    async def response(request):
        requests.append(request)
        await asyncio.sleep(.01)
        return httpx.Response(200,json={"usage":{"cost_rub":.02},"choices":[{"message":{"content":json.dumps({"reason":"Нет данных"})}}]})
    ai.client=httpx.AsyncClient(transport=httpx.MockTransport(response))
    await asyncio.gather(ai.call(Analysis,"system","input","j","t1"),ai.call(Analysis,"system","input","j","t2"))
    assert len(requests)==1 and store.cost("j")==.02
    await ai.close()


def test_api_import_exports_and_origin(cfg):
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/health").status_code==200
        res=client.post("/api/imports",files={"file":("test.xlsx",workbook_bytes())})
        assert res.status_code==200
        iid=res.json()["import_id"]
        assert len(client.get(f"/api/imports/{iid}/records").json())==3
        assert "source" not in client.get(f"/api/imports/{iid}/report.csv").text[:10]
        assert client.post("/api/jobs",json={"urls":["file:///etc/passwd"]}).status_code==400
        assert client.post("/api/jobs",json={"urls":["https://example.com"]},headers={"Origin":"https://evil.example"}).status_code==403
        assert client.get("/api/health",headers={"Host":"evil.example"}).status_code==403


def test_csv_formula_injection():
    assert safe_cell("=HYPERLINK(\"evil\")").startswith("'")
    assert safe_cell(" Обычная книга")==" Обычная книга"


async def test_agent_rejects_hallucinated_facts(cfg,store):
    from monitor.ai import Fact
    cfg.ai_enabled=True
    store.execute("INSERT INTO books VALUES(?,?,?,?,0,'[]')",("a","Ярроу","Айлин Лин","ebook"))
    store.create_job([{"url":"https://example.com/page"}],{"scope":"work"})
    engine=Engine(cfg,store)
    async def fetch(*args,**kwargs):
        return {"url":args[0],"http_status":200,"method":"http", "extracted":{"title":"Страница","author":"","format":"ebook","text":"Нет данных о книге", "links":[],"evidence":[],"extraction":"html_rules"}}
    async def analyze(*args):
        return Analysis(title=Fact(value="Ярроу",quote="Ярроу"),author=Fact(value="Айлин Лин",quote="Айлин Лин"),book_id="a",match_quote="Ярроу",reason="unsupported")
    engine.fetcher.fetch=fetch
    engine.ai.analyze=analyze
    cfg.browser_enabled=False
    result=await engine.process(store.claim())
    assert result["status"]==2
    assert result["extracted"]["title"]=="Страница"
    await engine.close()


def test_real_attachment_readonly(store):
    import os
    source=os.getenv("BOOK_MONITOR_TEST_WORKBOOK")
    if not source:
        pytest.skip("Optional local attachment: set BOOK_MONITOR_TEST_WORKBOOK")
    p=Path(source)
    if not p.exists():
        pytest.skip("User attachment not available")
    result=read_workbook(p.read_bytes(),p.name,store)
    assert result["sheets"]=={"Задание 1":150,"Задание 2":10}
    assert result["ambiguous_titles"]==2
