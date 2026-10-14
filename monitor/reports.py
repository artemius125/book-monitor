import csv
import io
import json


def task_report(store, job_id):
    rows = store.query("SELECT * FROM tasks WHERE job_id=? ORDER BY updated", (job_id,))
    for r in rows:
        r["result"] = json.loads(r["result"]) if r["result"] else None
        if r["record_id"]:
            source = store.query("SELECT sheet,row_number,raw FROM records WHERE id=?", (r["record_id"],))
            r["source"] = {**source[0], "raw": json.loads(source[0]["raw"])} if source else None
    return rows


def safe_cell(value):
    s = str(value if value is not None else "")
    return "'"+s if s.lstrip().startswith(("=", "+", "-", "@")) else s


def task_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["URL", "state", "status", "author", "title", "format", "book_id", "reason", "error", "source_sheet", "source_row", "evidence"])
    for row in rows:
        result = row["result"] or {}
        x = result.get("extracted") or {}
        book = (result.get("match") or {}).get("book") or {}
        source = row.get("source") or {}
        writer.writerow(map(safe_cell, [row["url"], row["state"], result.get("status"), x.get("author"), x.get("title"), x.get("format"), book.get("id"), result.get("reason"), result.get("error", {}).get("code"), source.get("sheet"), source.get("row_number"), json.dumps(x.get("evidence", []), ensure_ascii=False)]))
    return "\ufeff" + out.getvalue()


def records_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["record_id","sheet","row","raw","title","author","format","url","book_id","canonical_book_id","canonical_title","canonical_author","duplicate_url_rows","same_book_rows","grouping_status","grouping_notes","warnings"])
    for r in rows:
        p = json.loads(r["prepared"])
        writer.writerow(map(safe_cell, [r["id"],r["sheet"],r["row_number"],r["raw"],p["title"],p["author"],p["format"],p["url"],p["book_id"],p.get("canonical_book_id",p["book_id"]),p.get("canonical_title",p["title"]),p.get("canonical_author",p["author"]),json.dumps(p["duplicate_url_rows"]),json.dumps(p["same_book_rows"]),p.get("grouping_status",""),"; ".join(p.get("grouping_notes",[])),"; ".join(p["warnings"])]))
    return "\ufeff"+out.getvalue()
