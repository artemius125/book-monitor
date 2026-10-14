import io
import json
import openpyxl
import pytest
from fastapi.testclient import TestClient
from monitor.config import Settings
from monitor.store import Store
from monitor.ingest import read_workbook
from monitor.grouping import suggestions,merge,undo,decorated_records,group_detail
from monitor.matching import decide_match,collection_match
from monitor.api import create_app
from monitor.engine import Engine


@pytest.fixture
def store(tmp_path):
    cfg=Settings(_env_file=None,data_dir=tmp_path,ai_enabled=False)
    cfg.prepare()
    return Store(tmp_path)


def data(rows):
    wb=openpyxl.Workbook();ws=wb.active;ws.title="Books"
    ws.append(["Правообладатель","Автор","Название","Тип","url","Сайт"])
    for i,(title,author) in enumerate(rows):
        ws.append(["Demo",author,title,"ebook",f"https://example.com/book-{i}","example.com"])
    out=io.BytesIO();wb.save(out);return out.getvalue()


def test_exact_duplicates_have_one_card_with_all_sources(store):
    r=read_workbook(data([("Книга","Анна Волкова"),("Книга","Волкова Анна"),("Книга;","Анна Волкова")]),"demo.xlsx",store)
    b=store.books()[0]
    assert len(store.books())==1 and len(b["source_ids"])==3 and len(b["urls"])==3
    assert len(b["aliases"])==3 and b["title"]=="Книга"
    assert decide_match("Книга","Волкова Анна",store.books())["state"]=="matched"


def test_different_authors_are_only_suggested_then_confirmed(store):
    source=data([("Путь","Анна Волкова"),("Путь","Аня Волкова"),("Путь","А. Волкова")])
    r=read_workbook(source,"demo.xlsx",store)
    assert len(store.books())==3 and len(suggestions(store))==3
    a,b,c=store.books()
    assert any(row["prepared"]["warnings"] for row in decorated_records(store,r["import_id"]))
    merge(store,b["id"],a["id"],"Псевдоним подтверждён владельцем каталога")
    merge(store,c["id"],a["id"],"Это тот же автор по официальному ID")
    books=store.books()
    assert len(books)==1 and len(books[0]["source_ids"])==3
    for author in ["Анна Волкова","Аня Волкова","А. Волкова"]:
        m=decide_match("Путь",author,books)
        assert m["state"]=="matched" and m["book"]["id"]==a["id"]
    rows=decorated_records(store,r["import_id"])
    assert all(not row["prepared"]["warnings"] for row in rows)
    assert all(row["prepared"]["original_warnings"] for row in rows)
    assert {row["raw"]["Автор"] for row in rows}=={"Анна Волкова","Аня Волкова","А. Волкова"}
    assert {row["prepared"]["canonical_book_id"] for row in rows}=={a["id"]}
    read_workbook(source,"demo.xlsx",store)
    assert len(store.books())==1 and len(store.books()[0]["source_ids"])==3
    undo(store,c["id"])
    assert len(store.books())==2 and any(row["prepared"]["warnings"] for row in decorated_records(store,r["import_id"]))


def test_confirmed_title_alias_participates_in_collection_matching(store):
    read_workbook(data([("Полярный путь","Анна Волкова"),("Путь к полюсу","Аня Волкова")]),"demo.xlsx",store)
    a,b=store.books()
    merge(store,b["id"],a["id"],"Альтернативное название подтверждено каталогом")
    assert decide_match(b["title"],b["author"],store.books())["state"]=="matched"
    x={"collection_titles":[b["title"]],"author":b["author"]}
    assert collection_match(x,store.books(),b["id"])["book"]["id"]==a["id"]


def test_conflicting_volume_merges_rejected_and_no_cycles(store):
    read_workbook(data([("Хроники. Книга I","Анна Волкова"),("Хроники. Книга II","Анна Волкова")]),"demo.xlsx",store)
    a,b=store.books()
    assert not suggestions(store)
    with pytest.raises(ValueError,match="томов"):
        merge(store,b["id"],a["id"],"Похожая книга")
    with pytest.raises(ValueError,match="уже объединены"):
        merge(store,a["id"],a["id"],"same")
    assert len(store.books())==2


def test_authoritative_ids_are_not_overwritten(store):
    store.execute("INSERT INTO books VALUES('a','Путь','Анна','ebook',0,'[]')")
    store.execute("INSERT INTO books VALUES('b','Путь','Аня','ebook',0,'[]')")
    with pytest.raises(ValueError,match="Эталонные"):
        merge(store,"a","b","alias")


def test_review_merge_export_and_undo_api(tmp_path):
    cfg=Settings(_env_file=None,data_dir=tmp_path,ai_enabled=False)
    app=create_app(cfg)
    with TestClient(app) as client:
        r=client.post("/api/imports",files={"file":("demo.xlsx",data([("Путь","Анна Волкова"),("Путь","Аня Волкова")]))}).json()
        g=client.get("/api/book-groups").json()
        p=g["suggestions"][0]
        assert client.post("/api/book-merge-reviews/"+p["id"],json={"state":"rejected"}).status_code==200
        assert client.get("/api/book-groups").json()["suggestions"][0]["state"]=="rejected"
        assert client.post("/api/book-merge-reviews/"+p["id"],json={"state":"pending"}).status_code==200
        body={"source_id":p["right"]["id"],"target_id":p["left"]["id"],"reason":"Подтверждённый псевдоним"}
        assert client.post("/api/book-merges",json={**body,"reason":" "}).status_code==400
        assert client.post("/api/book-merges",json={**body,"author":"Неведомый Автор"}).status_code==400
        res=client.post("/api/book-merges",json=body)
        assert res.status_code==200 and len(res.json()["book"]["sources"])==2
        assert len(client.get("/api/book-groups").json()["books"])==1
        csv=client.get(f"/api/imports/{r['import_id']}/report.csv").text
        assert "canonical_author" in csv and "Одинаковое название у разных авторов" not in csv.splitlines()[-1].split(',')[-1]
        assert client.post("/api/book-merges/"+body["source_id"]+"/undo").status_code==200
        assert len(client.get("/api/book-groups").json()["books"])==2


async def test_page_check_uses_confirmed_alias_without_ai(store,tmp_path):
    read_workbook(data([("Путь","Анна Волкова"),("Путь","Псевдоним")]),"demo.xlsx",store)
    a,b=store.books()
    merge(store,b["id"],a["id"],"Официальный каталог подтверждает псевдоним")
    cfg=Settings(_env_file=None,data_dir=tmp_path,ai_enabled=False,browser_enabled=False)
    engine=Engine(cfg,store)
    store.create_job([{"url":"https://example.com/reader"}],{"scope":"work"})
    async def fetch(*args,**kwargs):
        return {"url":args[0],"http_status":200,"method":"http","extracted":{"title":b["title"],"author":b["author"],"format":"ebook","text":"Страница книги","links":[],"evidence":[{"kind":"reader_link","quote":"Читать онлайн","url":args[0]}],"extraction":"html_rules"}}
    engine.fetcher.fetch=fetch
    result=await engine.process(store.claim())
    assert result["status"]==1 and result["match"]["book"]["id"]==a["id"]
    assert result["match"]["book"]["matched_alias"]["author"]==b["author"]
    assert not any(s["action"]=="ai_analyze" for s in result["trace"])
    await engine.close()
