import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
from .ai import AI, AIUnavailable
from .extract import classify
from .fetch import Fetcher, FetchError
from .ingest import norm, author_key
from .matching import decide_match, rank_books, collection_match


class Engine:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.fetcher = Fetcher(cfg, store)
        self.ai = AI(cfg, store)
        self.workers = []
        self.stopping = False

    async def start(self):
        # A single service process owns the queue. Recover interrupted tasks on restart.
        self.store.execute("UPDATE tasks SET state='pending' WHERE state='running'")
        self.workers = [asyncio.create_task(self.worker(), name=f"worker-{i}") for i in range(self.cfg.concurrency)]

    async def close(self):
        self.stopping = True
        for t in self.workers:
            t.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.fetcher.close()
        await self.ai.close()

    async def worker(self):
        while not self.stopping:
            task = self.store.claim()
            if not task:
                await asyncio.sleep(.5)
                continue
            try:
                async with asyncio.timeout(150):
                    result = await self.process(task)
                self.store.finish(task["id"], "done" if result["status"] != 2 else "needs_attention", result)
                self.store.event("result", f"Статус {result['status']}: {result['reason']}", task["id"], "info" if result["status"] != 2 else "warning")
            except FetchError as e:
                await self.handle_error(task, e)
            except TimeoutError:
                await self.handle_error(task, FetchError("task_timeout", "Проверка превысила общий лимит времени", True))
            except asyncio.CancelledError:
                self.store.execute("UPDATE tasks SET state='pending' WHERE id=?", (task["id"],))
                raise
            except Exception as e:
                # Exception type is logged; secrets and response bodies are never logged.
                await self.handle_error(task, FetchError("internal_error", f"Внутренняя ошибка {type(e).__name__}"))

    async def process(self, task):
        options = json.loads(self.store.query("SELECT options FROM jobs WHERE id=?", (task["job_id"],))[0]["options"])
        raw_record = self.store.query("SELECT * FROM records WHERE id=?", (task["record_id"],)) if task["record_id"] else []
        expected = json.loads(raw_record[0]["prepared"]) if raw_record else {}
        books = self.store.books()
        page = await self.fetcher.fetch(task["url"], task["id"], fresh=task["attempts"]>1 or task["next_at"] == -1)
        trace = [{"action": "fetch", "method": page["method"], "url": page["url"], "cached": page.get("cached", False)}]
        if page.get("missing"):
            return {"status": 0, "reason": "Страница отсутствует (404/410)", "url": task["url"],
                    "http_status": page["http_status"], "match": None, "extracted": None, "trace": trace}
        extracted = page["extracted"]
        match = collection_match(extracted, books, expected.get("book_id")) or decide_match(extracted["title"], extracted["author"], books)
        visited = {page["url"]}
        ai_notes = []
        for step in range(self.cfg.max_agent_steps):
            if match["state"] == "matched" and extracted["evidence"]:
                break
            if not self.cfg.ai_enabled:
                break
            candidates = rank_books(extracted["title"] or expected.get("title", ""), extracted["author"], books)
            try:
                advice = await self.ai.analyze(extracted, candidates, task["job_id"], task["id"])
            except AIUnavailable as e:
                self.store.event("ai_unavailable", str(e), task["id"])
                ai_notes.append(str(e))
                break
            trace.append({"action": "ai_analyze", "step": step+1, "reason": advice.reason})
            text = extracted["text"]
            for name in ("title", "author"):
                fact = getattr(advice, name)
                if fact.value and fact.quote and fact.quote in text:
                    # The model may choose a substring/name from the cited text, not invent one.
                    value_tokens = norm(fact.value).split()
                    if value_tokens and all(tok in norm(fact.quote).split() for tok in value_tokens):
                        extracted[name] = fact.value
                        extracted.setdefault("ai_fact_evidence", {})[name] = fact.quote
            match = collection_match(extracted, books, expected.get("book_id")) or decide_match(extracted["title"], extracted["author"], books)
            selected = next((b for b in candidates if b["id"] == advice.book_id), None)
            if selected and advice.match_quote and advice.match_quote in text and extracted.get("ai_fact_evidence", {}).get("title") and extracted.get("ai_fact_evidence", {}).get("author"):
                ranked = rank_books(extracted["title"], extracted["author"], [selected])
                b = ranked[0]
                if b["title_score"]>=75 and b["author_score"]>=80 and not b["volume_conflict"]:
                    match = {"state": "matched", "book": selected, "candidates": candidates, "reason": "AI сопоставил подтверждённые цитатами название и автора"}
            reader_context = "reader" in page["url"].lower() or extracted["extraction"] == "pdf" or (len(text)>4000 and re.search(r"(?:Глава|Chapter)\s+(?:\d+|[IVX]+|первая)", text, re.I))
            if reader_context and advice.content_kind == "book_text" and len(advice.content_quote) >= 180 and advice.content_quote in text:
                extracted["evidence"].append({"kind": "book_text", "quote": advice.content_quote, "url": page["url"], "verified": "exact_quote_ai_interpretation"})
            if match["state"] == "matched" and extracted["evidence"]:
                break
            # Bounded read-only agent tool: follow an observed link on the same host.
            observed = {l["url"] for l in extracted["links"]}
            target = advice.next_url
            if target and target in observed and target not in visited and urlsplit(target).hostname == urlsplit(task["url"]).hostname:
                if any(x in target.lower() for x in ("download", "logout", "register", "signup", "login")):
                    break
                next_page = await self.fetcher.fetch(target, task["id"])
                visited.add(target)
                trace.append({"action": "follow_observed_link", "url": target})
                if not next_page.get("missing"):
                    nxt = next_page["extracted"]
                    # Carry main-card metadata and preserve source URLs on evidence.
                    nxt["title"] = nxt["title"] or extracted["title"]
                    nxt["author"] = nxt["author"] or extracted["author"]
                    nxt["evidence"] += extracted["evidence"]
                    nxt["text"] = (extracted["text"][:8000] + "\n\nFOLLOWED PAGE:\n" + nxt["text"][:12000])
                    extracted = nxt
                    page = next_page
                    match = collection_match(extracted, books, expected.get("book_id")) or decide_match(extracted["title"], extracted["author"], books)
                continue
            if page["method"] == "http" and self.cfg.browser_enabled and not extracted["evidence"]:
                page = await self.fetcher.fetch(task["url"], task["id"], force_browser=True)
                if not page.get("missing"):
                    extracted = page["extracted"]
                    match = collection_match(extracted, books, expected.get("book_id")) or decide_match(extracted["title"], extracted["author"], books)
                trace.append({"action": "render_fallback", "method": "playwright"})
                continue
            break
        result = classify(extracted, match, expected.get("format"), options.get("scope", "work"))
        result.update(url=task["url"], final_url=page["url"], http_status=page["http_status"], trace=trace, ai_notes=ai_notes,
                      record_id=task["record_id"], catalog_provisional=bool(match.get("book", {}).get("provisional")) if match.get("book") else None)
        # Keep evidence and extracted facts; avoid duplicating the full page in each report.
        artifact = f"{task['id']}.json"
        (self.cfg.data_dir / "evidence" / artifact).write_text(json.dumps({"url": page["url"], "text": extracted["text"], "trace": trace}, ensure_ascii=False, indent=2))
        result["evidence_artifact"] = artifact
        result["extracted"] = {k:v for k,v in extracted.items() if k not in ("text", "links")}
        if ai_notes and result["status"] == 2:
            result["reason"] += "; " + ai_notes[-1]
        return result

    async def handle_error(self, task, error):
        retry = error.retryable and task["attempts"] < self.cfg.max_attempts
        delay = max(error.retry_after, min(5*2**(task["attempts"]-1), 120))
        state = "retry" if retry else "needs_attention"
        result = {"status": 2, "reason": str(error), "url": task["url"], "http_status": error.http_status,
                  "error": {"code": error.code, "retryable": error.retryable, "attempt": task["attempts"],
                            "retry_in_seconds": delay if retry else None}, "screenshot": error.artifact,
                  "next_action": "Повтор запланирован" if retry else "Проверьте причину; после исправления нажмите Повторить"}
        if error.code in ("captcha", "auth_required"):
            result["next_action"] = "Откройте ручную сессию, решите CAPTCHA / войдите, сохраните сессию и повторите проверку"
        self.store.finish(task["id"], state, result, time.time()+delay if retry else 0)
        self.store.event(error.code, str(error)+("; повтор через %.0f с" % delay if retry else "; требуется внимание"), task["id"])


def records_for_job(store, import_id, sheet=None):
    rows = store.query("SELECT * FROM records WHERE import_id=?" + (" AND sheet=?" if sheet else ""), (import_id, sheet) if sheet else (import_id,))
    return [{"id": r["id"], "url": json.loads(r["prepared"])["url"]} for r in rows]
