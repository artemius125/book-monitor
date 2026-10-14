import re
from rapidfuzz import fuzz
from .ingest import norm, author_key


def volumes(title):
    words = {"первая":1,"первый":1,"вторая":2,"второй":2,"третья":3,"третий":3,"четвертая":4,"четвертый":4,"пятая":5,"пятый":5}
    tokens = re.findall(r"(?:том|книга|часть|vol(?:ume)?|book)\s+(\d+|[ivxlcdm]+|первая|первый|вторая|второй|третья|третий|четвертая|четвертый|пятая|пятый)\b", norm(title))
    out=set()
    for token in tokens:
        if token.isdigit():
            out.add(str(int(token)))
        elif token in words:
            out.add(str(words[token]))
        else:
            values={"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
            number=0; previous=0
            for ch in reversed(token):
                value=values[ch]
                number += -value if value < previous else value
                previous=max(previous,value)
            out.add(str(number))
    return out


def rank_books(title, author, books, limit=5):
    tn, an = norm(title), author_key(author)
    result = []
    for b in books:
        bt, ba = norm(b["title"]), author_key(b["author"])
        ts = fuzz.ratio(tn, bt)
        aus = fuzz.ratio(an, ba) if an else 0
        conflict = bool(volumes(title) and volumes(b["title"]) and volumes(title) != volumes(b["title"]))
        score = .72*ts + .28*aus if an else .72*ts
        result.append({**b, "title_score": round(ts, 1), "author_score": round(aus, 1),
                       "score": round(score, 1), "volume_conflict": conflict})
    return sorted(result, key=lambda b: (not b["volume_conflict"], b["score"]), reverse=True)[:limit]


def decide_match(title, author, books):
    ranked = rank_books(title, author, books)
    if not title or not ranked:
        return {"state": "unknown", "book": None, "candidates": ranked, "reason": "Нет извлечённого названия или каталога"}
    best = ranked[0]
    # Equal title+author in different formats is one work, not an ambiguity.
    competitors = [b for b in ranked[1:] if (norm(b["title"]), author_key(b["author"])) != (norm(best["title"]), author_key(best["author"]))]
    gap = best["score"] - competitors[0]["score"] if competitors else 100
    if not best["volume_conflict"] and best["title_score"] >= 94 and best["author_score"] >= 94 and gap >= 5:
        return {"state": "matched", "book": best, "candidates": ranked, "reason": "Название и автор совпадают; номер тома не противоречит"}
    if best["title_score"] < 55:
        if not author:
            return {"state": "unknown", "book": None, "candidates": ranked, "reason": "Не извлечён автор; содержимое недостаточно для отрицательного вывода"}
        return {"state": "no_match", "book": None, "candidates": ranked, "reason": "Нет близких названий в каталоге"}
    return {"state": "ambiguous", "book": None, "candidates": ranked, "reason": "Недостаточно данных или близкие кандидаты"}


def collection_match(extracted, books, preferred_id=None):
    titles = extracted.get("collection_titles", [])
    if not titles or not extracted["author"]:
        return None
    found = []
    for b in books:
        if fuzz.ratio(author_key(b["author"]), author_key(extracted["author"])) < 94:
            continue
        if any(norm(t) == norm(b["title"]) for t in titles):
            found.append(b)
    if not found:
        return None
    selected = next((b for b in found if b["id"] == preferred_id), found[0])
    return {"state": "matched", "book": selected, "candidates": found,
            "reason": "Автор совпал; книга явно перечислена в составе сборника", "collection": True,
            "title_quote": next(t for t in titles if norm(t) == norm(selected["title"]))}
