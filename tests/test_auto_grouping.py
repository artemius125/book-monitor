import asyncio
import io
import json
import time
from types import SimpleNamespace
import openpyxl
import pytest
from fastapi.testclient import TestClient
from monitor.api import create_app
from monitor.ai import GroupAnalysis, AIUnavailable
from monitor.auto_grouping import AutoGrouper
from monitor.config import Settings
from monitor.engine import Engine
from monitor.grouping import suggestions, undo, decorated_records
from monitor.ingest import read_workbook
from monitor.matching import decide_match
from monitor.store import Store


def workbook(authors):
    wb=openpyxl.Workbook();ws=wb.active
    ws.append(['Правообладатель','Автор','Название','Тип','URL','Сайт'])
    for i,a in enumerate(authors):
        ws.append(['Demo',a,'Полярный путь','ebook',f'https://example.com/{i}','example.com'])
    stream=io.BytesIO();wb.save(stream)
    return stream.getvalue()


@pytest.fixture
def setup(tmp_path):
    cfg=Settings(_env_file=None,data_dir=tmp_path,ai_enabled=True,browser_enabled=False)
    cfg.prepare();store=Store(tmp_path)
    return cfg,store


def resolver(setup,decision,text=''):
    cfg,store=setup;calls=[]
    async def analyze(pair,sources,job_id):
        calls.append(pair['id'])
        if isinstance(decision,Exception):
            raise decision
        return GroupAnalysis.model_validate(decision)
    async def fetch(url,*args,**kwargs):
        return {'url':url,'extracted':{'text':text}}
    return AutoGrouper(cfg,store,SimpleNamespace(analyze_group=analyze),SimpleNamespace(fetch=fetch)),calls


def same(**kwargs):
    return {'decision':'same','confidence':.99,'basis':'spelling','reason':'Опечатка в имени',**kwargs}


async def test_automatic_typo_merge_is_used_and_reimport_does_not_rebill(setup):
    cfg,store=setup;data=workbook(['Анна Волкова','Анна Волквоа'])
    report=read_workbook(data,'input.xlsx',store)
    raw_before=store.query('SELECT raw FROM records ORDER BY id')
    grouper,calls=resolver(setup,same(author='Анна Волкова',confidence=.9))
    await grouper.run('import:1')
    assert len(store.books())==1 and len(calls)==1
    assert decide_match('Полярный путь','Анна Волквоа',store.books())['state']=='matched'
    assert store.query('SELECT raw FROM records ORDER BY id')==raw_before
    read_workbook(data,'input.xlsx',store);await grouper.run('import:2')
    assert len(calls)==1
    merge=store.query('SELECT * FROM book_merges')[0]
    undo(store,merge['source_id']);await grouper.run('import:3')
    assert len(store.books())==2 and len(calls)==1


@pytest.mark.parametrize('decision',[
    same(),same(confidence=.5),same(basis='source',evidence=[{'url':'https://example.com/0','quote':'Вымышленная цитата, которой нет в источнике'}]),
    {'decision':'uncertain','confidence':.99,'basis':'insufficient','reason':'Нет доказательств'},
])
async def test_same_title_different_authors_not_blindly_merged(setup,decision):
    _,store=setup;read_workbook(workbook(['Анна Волкова','Мария Авенская']),'input.xlsx',store)
    grouper,calls=resolver(setup,decision,text='Описание книги без сведений о псевдонимах')
    await grouper.run('groups:1');await grouper.run('groups:2')
    assert len(store.books())==2 and len(calls)==1
    assert suggestions(store)[0]['state']=='uncertain'


async def test_verified_alias_quote_allows_automatic_merge(setup):
    _,store=setup;read_workbook(workbook(['Анна Волкова','Мария Авенская']),'input.xlsx',store)
    text='Анна Волкова — псевдоним автора Мария Авенская. Книга «Полярный путь».'
    grouper,_=resolver(setup,same(basis='source',evidence=[{'url':'https://example.com/0','quote':text}]),text)
    await grouper.run('groups:1')
    assert len(store.books())==1
    saved=json.loads(store.query('SELECT decision FROM ai_group_decisions')[0]['decision'])
    assert saved['evidence'][0]['quote']==text


async def test_negative_alias_statement_does_not_merge(setup):
    _,store=setup;read_workbook(workbook(['Анна Волкова','Мария Авенская']),'input.xlsx',store)
    text='Анна Волкова не является псевдонимом автора Мария Авенская.'
    grouper,_=resolver(setup,same(basis='source',evidence=[{'url':'https://example.com/0','quote':text}]),text)
    await grouper.run('groups:1')
    assert len(store.books())==2 and suggestions(store)[0]['state']=='uncertain'


async def test_different_authors_automatically_separated(setup):
    _,store=setup;report=read_workbook(workbook(['Анна Волкова','Мария Авенская']),'input.xlsx',store)
    grouper,_=resolver(setup,{'decision':'different','confidence':.99,'basis':'source','reason':'Разные полные имена авторов'})
    await grouper.run('groups:1')
    assert suggestions(store)[0]['state']=='different' and len(store.books())==2
    rows=decorated_records(store,report['import_id'])
    assert all(r['prepared']['grouping_status']=='distinct' and not r['prepared']['warnings'] for r in rows)
    assert all(r['prepared']['original_warnings'] for r in rows)


async def test_budget_failure_deferred_once_and_processing_continues(setup):
    cfg,store=setup;read_workbook(workbook(['Анна Волкова','Мария Авенская','Ольга Полярная']),'input.xlsx',store)
    grouper,calls=resolver(setup,AIUnavailable('Лимит расходов'))
    result=await grouper.run('groups:1');await grouper.run('groups:2')
    assert result['remaining']==0 and len(calls)==1
    assert all(p['state']=='deferred' for p in suggestions(store))


async def test_manual_rejection_wins_over_inflight_ai(setup):
    _,store=setup;read_workbook(workbook(['Анна Волкова','Анна Волково']),'input.xlsx',store)
    async def analyze(pair,sources,job_id):
        store.execute('INSERT INTO merge_reviews VALUES(?,?,?,?)',(pair['id'],'rejected','Разные люди',0))
        return GroupAnalysis.model_validate(same())
    grouper=AutoGrouper(setup[0],store,SimpleNamespace(analyze_group=analyze),None)
    await grouper.run('groups:1')
    assert len(store.books())==2


async def test_engine_waits_for_grouping_before_page_workers(setup):
    cfg,store=setup;read_workbook(workbook(['Анна Волкова','Анна Волково']),'input.xlsx',store)
    engine=Engine(cfg,store);gate=asyncio.Event();processed=[]
    async def analyze(*args):
        await gate.wait();return GroupAnalysis.model_validate(same())
    engine.ai.analyze_group=analyze
    async def process(task):
        processed.append(len(store.books()));return {'status':1,'reason':'ok'}
    engine.process=process
    store.create_job([{'url':'https://example.com/check'}],{})
    await engine.start()
    try:
        await asyncio.sleep(.05);assert not processed
        gate.set();await asyncio.wait_for(engine.grouping_ready.wait(),2)
        for _ in range(20):
            if processed:break
            await asyncio.sleep(.05)
        assert processed==[1]
    finally:
        await engine.close()


def test_import_api_runs_ai_and_retry_only_reopens_unresolved(setup):
    cfg,_=setup;app=create_app(cfg);calls=[];confidence=[.5]
    async def analyze(*args):
        calls.append(1);return GroupAnalysis.model_validate(same(confidence=confidence[0]))
    app.state.engine.ai.analyze_group=analyze
    def completed(client):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            data=client.get('/api/book-groups').json()
            if not data['automation']['running']:
                return data
            time.sleep(.02)
        raise AssertionError('Grouping did not finish')
    with TestClient(app) as client:
        r=client.post('/api/imports',files={'file':('input.xlsx',workbook(['Анна Волкова','Анна Волквоа']))})
        assert r.status_code==200
        data=completed(client);assert data['suggestions'][0]['state']=='uncertain'
        confidence[0]=.99
        assert client.post('/api/book-groups/analyze').status_code==200
        data=completed(client);assert len(data['books'])==1 and len(calls)==2
        detail=client.get('/api/book-groups/'+data['books'][0]['id']).json()
        assert detail['ai_history'][0]['state']=='merged'
        source=detail['merges'][0]['source_id']
        assert client.post('/api/book-merges/'+source+'/undo').status_code==200
        assert client.post('/api/book-groups/analyze').status_code==200
        data=completed(client);assert len(data['books'])==2 and len(calls)==2
