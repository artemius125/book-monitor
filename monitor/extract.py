import io
import json
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from pypdf import PdfReader


def access_problem(html, status=200):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    title = soup.title.get_text().lower() if soup.title else ""
    # Detect challenge walls, not every site that includes a CAPTCHA in comments.
    wall = bool(re.search(r"just a moment|checking your browser|verify you are human|проверка браузера|подтвердите.*(?:человек|робот)|security verification|access denied", title + " " + text[:1800]))
    challenge = soup.select_one("#challenge-form, #cf-challenge-running, form[action*='challenge'], .cf-turnstile, .g-recaptcha, iframe[src*='captcha']")
    if wall or (challenge and len(text) < 1000):
        return "captcha", "Обнаружена проверка человека / защитная страница"
    if len(text)<800 and re.search(r"не удалось загрузить страницу|для продолжения включите javascript|enable javascript|loading\.\.\.", text):
        return "js_required", "Вместо содержимого получена оболочка загрузки JavaScript"
    gate = re.search(r"(?:для|чтобы)\s+(?:просмотра|чтения|доступа|читать|скачать).*?(?:войдите|зарегистриру|регистрац|авториз)|(?:войдите|зарегистрируйтесь).*?чтобы\s+(?:читать|скачать)", text[:1600])
    if status in (401, 407) or gate or (soup.select_one("input[type=password]") and len(text) < 1800):
        return "auth_required", "Нужен вход или регистрация; требуется сессия пользователя"
    if status == 429:
        return "rate_limit", "Сайт ограничил частоту запросов"
    if status == 403:
        return "blocked", "Сервер запретил доступ (403)"
    if status >= 500:
        return "server_error", f"Ошибка сервера {status}"
    return None


def primary_root(soup):
    root = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    root = BeautifulSoup(str(root), "html.parser")
    for e in root.select("nav,footer,aside,form,.comments,#comments,.related,.related-posts,.recommendations"):
        e.decompose()
    for h in list(root.select("h2,h3,h4")):
        if h.parent and re.search(r"^(?:Похожие|Еще книги|Ещё книги|Читать другие книги|Комментарии)", h.get_text(strip=True), re.I):
            for following in list(h.next_siblings):
                if hasattr(following, "decompose"):
                    following.decompose()
                else:
                    following.extract()
            h.decompose()
    return root


def primary_text(soup):
    root = primary_root(soup)
    for e in root.select("script,style"):
        e.decompose()
    text = root.get_text("\n", strip=True)
    text = re.split(r"\n(?:Похожие (?:книги|записи|аудиокниги)|Еще книги автора|Ещё книги автора|Читать другие книги|Комментарии)\b", text, maxsplit=1, flags=re.I)[0]
    return text[:22000]


def html_extract(html, url):
    soup = BeautifulSoup(html, "html.parser")
    text = primary_text(soup)
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    author = ""
    evidence = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            objects = json.loads(script.get_text())
            if isinstance(objects, dict):
                objects = objects.get("@graph", [objects])
            for obj in objects if isinstance(objects, list) else []:
                if not isinstance(obj, dict) or obj.get("@type") not in ("Book", "Audiobook"):
                    continue
                title = obj.get("name") or title
                a = obj.get("author", "")
                if isinstance(a, list):
                    a = a[0] if a else ""
                author = a.get("name", "") if isinstance(a, dict) else str(a)
        except (ValueError, TypeError):
            pass
    if not author:
        m = re.search(r"(?:Автор(?:ы)?|Author)\s*[:：]\s*([^\n]{2,160})", text, re.I)
        if m:
            author = m.group(1).strip()
    if not author and title:
        parts = re.split(r"\s+[—–]\s+", title)
        if len(parts) == 2:
            title, author = parts
    elif author and title:
        parts = re.split(r"\s+[—–]\s+", title)
        if len(parts)==2:
            from .ingest import author_key
            if author_key(parts[1]) == author_key(author):
                title=parts[0]
    links = []
    # Capture content evidence only in the primary section, excluding global recommendations.
    root = primary_root(soup)
    for a in root.select("a[href]"):
        label = a.get_text(" ", strip=True)[:180]
        target = urljoin(url, a.get("href", ""))
        if urlsplit(target).scheme not in ("https", "http", "magnet"):
            continue
        links.append({"text": label, "url": target})
        if a.find_parent(class_=re.compile("related|recommend|comment", re.I)):
            continue
        if re.search(r"(?:\.torrent|magnet:|\b(?:FB2|EPUB|TXT|PDF)\b|скачать.*(?:книг|torrent)|download)", label + " " + target, re.I) or re.match(r"^скачать(?:\s|$)", label, re.I):
            evidence.append({"kind": "download_link", "quote": label, "url": target, "verified": "dom"})
        if re.search(r"читать онлайн|читать книгу|read online", label, re.I):
            evidence.append({"kind": "reader_link", "quote": label, "url": target, "verified": "dom"})
    audio = root.select("audio, iframe[src*='player'], iframe[id*='player'], [id*='playerjs']")
    if audio:
        evidence.append({"kind": "audio_player", "quote": "Аудиоплеер / iframe в основной карточке", "url": url, "verified": "dom"})
    fmt = "audiobook" if audio or re.search(r"жанр аудиокниги|год начитки|чтец\s*:|читает\s*:", text, re.I) else "ebook"
    collection_titles = []
    if re.search(r"сборник|собрание", title, re.I) and re.search(r"список книг", text, re.I):
        section = re.split(r"список книг\s*:", text, maxsplit=1, flags=re.I)[-1]
        section = re.split(r"\n(?:Издательство|Формат|Год|Скачать|Жанр)\s*[:\n]", section, maxsplit=1, flags=re.I)[0]
        collection_titles = [re.sub(r"^\d+[.\s]+", "", line).strip() for line in section.splitlines() if re.match(r"^\d+\.\s", line)]
    return {"title": str(title)[:500], "author": str(author)[:300], "format": fmt, "text": text,
            "evidence": evidence[:30], "links": links[:100], "extraction": "html_rules",
            "coverage": "primary_section", "collection_titles": collection_titles}


def pdf_extract(data, url):
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("PDF зашифрован")
    text = "\n".join(p.extract_text() or "" for p in reader.pages[:12])[:22000]
    title = str((reader.metadata or {}).get("/Title", ""))
    author = str((reader.metadata or {}).get("/Author", ""))
    if not text.strip():
        raise ValueError("PDF без текстового слоя: требуется OCR")
    return {"title": title, "author": author, "format": "ebook", "text": text,
            "evidence": [{"kind": "direct_pdf", "quote": text[:350], "url": url, "verified": "pdf_text"}],
            "links": [], "extraction": "pdf", "coverage": f"first_{min(12,len(reader.pages))}_pages"}


def classify(extracted, match, expected_format=None, scope="work"):
    evidence = extracted.get("evidence", [])
    mismatch = bool(expected_format and extracted.get("format") != expected_format)
    if match["state"] == "matched":
        if scope == "format" and mismatch:
            status, reason = 0, "Книга совпала, но найден другой формат контента"
        elif evidence:
            status, reason = 1, ("Книга явно включена в сборник; найдены признаки размещения" if match.get("collection") else "Автор и название совпали; найдены признаки размещения")
        else:
            status, reason = 0, "Книга совпала, признаков размещения в доступной странице нет"
    elif match["state"] == "no_match":
        status, reason = 0, "Содержимое не сопоставлено с каталогом"
    else:
        status, reason = 2, match["reason"]
    return {"status": status, "reason": reason, "match": match, "extracted": extracted,
            "format_mismatch": mismatch, "scope": scope,
            "interpretation": "Признаки размещения, не юридический вывод о нарушении прав"}
