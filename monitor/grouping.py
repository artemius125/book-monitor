"""Non-destructive, operator-confirmed book groups and provenance-preserving aliases."""
import json
import re
import time
from collections import Counter, defaultdict
from rapidfuzz import fuzz
from .ingest import norm, author_key, stable_id
from .matching import volumes

AMBIGUOUS = "Одинаковое название у разных авторов: требуется проверка каталога"


def resolve(book_id, links):
    seen = set()
    while book_id in links:
        if book_id in seen:
            raise ValueError("Цикл объединений книг")
        seen.add(book_id)
        book_id = links[book_id]
    return book_id


def graph(store):
    ids = {r["id"] for r in store.query("SELECT id FROM books")}
    return {r["source_id"]:r["target_id"] for r in store.query("SELECT * FROM book_merges")
            if r["source_id"] in ids and r["target_id"] in ids}


def canonical_books(store):
    base = store.query("SELECT * FROM books ORDER BY id")
    links = graph(store)
    profiles = {r["book_id"]:r for r in store.query("SELECT * FROM book_profiles")}
    records = {r["id"]:json.loads(r["prepared"]) for r in store.query("SELECT id,prepared FROM records")}
    grouped = defaultdict(list)
    for b in base:
        b["source_ids"] = json.loads(b["source_ids"])
        grouped[resolve(b["id"],links)].append(b)
    result = []
    for bid, members in grouped.items():
        primary = next(b for b in members if b["id"] == bid)
        sources = sorted({rid for b in members for rid in b["source_ids"]})
        pairs = {(b["title"],b["author"]) for b in members}
        source_pairs = [(records[r]["title"],records[r]["author"]) for r in sources if r in records]
        pairs.update(source_pairs)
        title, author = primary["title"], primary["author"]
        if primary["provisional"] and source_pairs:
            title = Counter(t for t,a in source_pairs).most_common(1)[0][0].strip(" ;\t\r\n")
            author = Counter(a for t,a in source_pairs).most_common(1)[0][0].strip(" ;\t\r\n")
        if bid in profiles:
            title, author = profiles[bid]["title"], profiles[bid]["author"]
        pairs.add((title,author))
        result.append({**primary,"title":title,"author":author,"source_ids":sources,
                       "member_ids":sorted(b["id"] for b in members),
                       "aliases":[{"title":t,"author":a} for t,a in sorted(pairs)],
                       "formats":sorted({b["format"] for b in members}),
                       "urls":sorted({records[r]["url"] for r in sources if r in records}),
                       "merge_count":len(members)-1})
    return sorted(result,key=lambda b:(norm(b["title"]),b["id"]))


def incompatible(left,right):
    for a in left.get("aliases",[left]):
        for b in right.get("aliases",[right]):
            va,vb = volumes(a["title"]),volumes(b["title"])
            if va and vb and va != vb:
                return True
            # Also protect volumes written as "Егерь-2" and other meaningful numbers.
            na,nb = set(re.findall(r"\d+",norm(a["title"]))),set(re.findall(r"\d+",norm(b["title"])))
            if not va and not vb and na and nb and na != nb:
                return True
    return False


def suggestions(store):
    books = canonical_books(store)
    reviews = {r["pair_id"]:r for r in store.query("SELECT * FROM merge_reviews")}
    # Token blocking limits candidate work for large catalogs; no all-pairs LLM calls.
    buckets = defaultdict(set)
    for i,b in enumerate(books):
        for alias in b["aliases"]:
            tokens = {t for t in norm(alias["title"]).split() if len(t)>=4}
            for token in tokens or {norm(alias["title"])}:
                buckets[token].add(i)
    pairs = set()
    for indexes in buckets.values():
        # Generic tokens can occur in thousands of books; use other, narrower blocks.
        if len(indexes)>300:
            continue
        ordered = sorted(indexes)
        pairs.update((a,b) for pos,a in enumerate(ordered) for b in ordered[pos+1:])
    # Exact titles must be considered even when their tokens are short or generic.
    exact = defaultdict(set)
    for i,b in enumerate(books):
        for alias in b["aliases"]:
            exact[norm(alias["title"])].add(i)
    for indexes in exact.values():
        ordered = sorted(indexes)
        pairs.update((a,b) for pos,a in enumerate(ordered) for b in ordered[pos+1:])
    out=[]
    for li,ri in sorted(pairs):
        left,right = books[li],books[ri]
        if incompatible(left,right):
            continue
        score=max((fuzz.ratio(norm(a["title"]),norm(b["title"])),
                   fuzz.ratio(author_key(a["author"]),author_key(b["author"])))
                  for a in left["aliases"] for b in right["aliases"])
        ts,aus=score
        same = any(norm(a["title"])==norm(b["title"]) for a in left["aliases"] for b in right["aliases"])
        if not same and not(ts>=90 and aus>=88):
            continue
        pid=stable_id(*sorted([left["id"],right["id"]]))
        reason="Одинаковое название; авторы могут быть разными людьми" if aus<94 else "Похожее название или вариант написания автора"
        if same and aus==100:
            reason="Одинаковые автор и название; разные записи или форматы"
        review=reviews.get(pid,{})
        # A prior rejection survives later merging of either side with another group.
        rejected = any(reviews.get(stable_id(*sorted([a,b])),{}).get("state")=="rejected"
                       for a in left["member_ids"] for b in right["member_ids"])
        out.append({"id":pid,"left":left,"right":right,"title_score":round(ts,1),
                    "author_score":round(aus,1),"reason":reason,
                    "state":"rejected" if rejected else review.get("state","pending")})
    return out


def merge(store,source_id,target_id,reason,title=None,author=None):
    if not reason.strip():
        raise ValueError("Укажите основание: почему это одна книга / один автор")
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        # All decisions and graph writes share this transaction.
        books=canonical_books(store)
        links=graph(store)
        source_id,target_id=resolve(source_id,links),resolve(target_id,links)
        if source_id==target_id:
            raise ValueError("Записи уже объединены")
        source=next((b for b in books if b["id"]==source_id),None)
        target=next((b for b in books if b["id"]==target_id),None)
        if not source or not target:
            raise ValueError("Книга не найдена")
        if not source["provisional"] or not target["provisional"]:
            raise ValueError("Эталонные ID не объединяются: исправьте каталог у его владельца")
        if incompatible(source,target):
            raise ValueError("Номера томов / значимые числа противоречат: это разные книги")
        chosen_title=(title if title is not None else target["title"]).strip()
        chosen_author=(author if author is not None else target["author"]).strip()
        if not chosen_title or not chosen_author:
            raise ValueError("Основные название и автор не могут быть пустыми")
        # Canonical presentation is selected from observed values, not invented metadata.
        allowed=source["aliases"]+target["aliases"]
        if norm(chosen_title) not in {norm(a["title"]) for a in allowed} or author_key(chosen_author) not in {author_key(a["author"]) for a in allowed}:
            raise ValueError("Выберите название и автора из исходных вариантов")
        db.execute("INSERT INTO book_merges VALUES(?,?,?,?)",(source_id,target_id,reason.strip(),time.time()))
        db.execute("INSERT OR REPLACE INTO book_profiles VALUES(?,?,?)",(target_id,chosen_title,chosen_author))
    store.event("books_merged",f"Подтверждено объединение: {chosen_title}. Основание: {reason.strip()}",level="info")
    return target_id


def undo(store,source_id):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM book_merges WHERE source_id=?",(source_id,)).fetchone()
        if not row:
            raise ValueError("Объединение не найдено")
        db.execute("DELETE FROM book_merges WHERE source_id=?",(source_id,))
        # Drop the merged presentation; surviving groups re-derive it from their sources.
        db.execute("DELETE FROM book_profiles WHERE book_id=?",(resolve(row["target_id"],graph(store)),))
    store.event("merge_undone","Объединение отменено; исходные записи восстановлены",level="info")


def decorated_records(store,import_id):
    books=canonical_books(store)
    mapping={mid:b for b in books for mid in b["member_ids"]}
    rows=store.query("SELECT * FROM records WHERE import_id=? ORDER BY sheet,row_number",(import_id,))
    title_groups=defaultdict(set)
    for b in books:
        for alias in b["aliases"]:
            title_groups[norm(alias["title"])].add(b["id"])
    for r in rows:
        r["raw"]=json.loads(r["raw"])
        p=r["prepared"]=json.loads(r["prepared"])
        b=mapping.get(p["book_id"])
        p["original_warnings"]=list(p["warnings"])
        if b:
            p.update(canonical_book_id=b["id"],canonical_title=b["title"],canonical_author=b["author"],
                     same_book_rows=[rid for rid in b["source_ids"] if rid!=r["id"]])
            p["warnings"]=[w for w in p["warnings"] if w!=AMBIGUOUS]
            if len(title_groups[p["title_key"]])>1:
                p["warnings"].append(AMBIGUOUS)
    return rows


def group_detail(store,book_id):
    links=graph(store)
    bid=resolve(book_id,links)
    b=next((b for b in canonical_books(store) if b["id"]==bid),None)
    if not b:
        raise ValueError("Книга не найдена")
    ids=set(b["source_ids"])
    b["sources"]=[{**r,"raw":json.loads(r["raw"]),"prepared":json.loads(r["prepared"])}
                  for r in store.query("SELECT * FROM records ORDER BY import_id,sheet,row_number") if r["id"] in ids]
    b["merges"]=[r for r in store.query("SELECT * FROM book_merges") if resolve(r["source_id"],links)==bid]
    return b
