import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from collections import defaultdict
from urllib.parse import urlsplit, urljoin
import httpx
from .extract import access_problem, html_extract, pdf_extract


class FetchError(Exception):
    def __init__(self, code, message, retryable=False, retry_after=0, artifact=None, http_status=None):
        super().__init__(message)
        self.code, self.retryable, self.retry_after, self.artifact, self.http_status = code, retryable, retry_after, artifact, http_status


async def validate_url(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise FetchError("invalid_url", "Допустимы публичные HTTP(S) адреса без пароля на стандартных портах")
    try:
        ips = await asyncio.to_thread(socket.getaddrinfo, p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError("dns_error", "Домен не разрешается в DNS", True)
    if not ips or any(not ipaddress.ip_address(item[4][0]).is_global for item in ips):
        raise FetchError("unsafe_url", "Внутренние, служебные и loopback адреса запрещены")
    return p.hostname


class Fetcher:
    def __init__(self, settings, store):
        self.cfg, self.store = settings, store
        self.locks = defaultdict(asyncio.Lock)
        self.last_request = {}
        self.client = httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False,
                                       headers={"User-Agent": "BookMonitor/0.1 (+local research; contact operator)"}, trust_env=False)
        self.pw = self.browser = None
        self.browser_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()
        if self.browser:
            try:
                await self.browser.close()
            except RuntimeError:
                # SIGINT may close the Playwright pipe before lifespan cleanup.
                pass
        if self.pw:
            try:
                await self.pw.stop()
            except RuntimeError:
                pass

    def session_path(self, domain):
        return self.cfg.data_dir / "sessions" / (hashlib.sha256(domain.encode()).hexdigest() + ".json")

    async def pace(self, domain):
        async with self.locks[domain]:
            wait = self.cfg.domain_delay - (time.monotonic()-self.last_request.get(domain, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request[domain] = time.monotonic()

    def cookie_jar(self, domain):
        path = self.session_path(domain)
        cookies = httpx.Cookies()
        if path.exists():
            for c in json.loads(path.read_text()).get("cookies", []):
                cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        return cookies

    async def http(self, url):
        current = url
        for _ in range(6):
            domain = await validate_url(current)
            await self.pace(domain)
            try:
                async with self.client.stream("GET", current, cookies=self.cookie_jar(domain)) as r:
                    if r.is_redirect:
                        if "location" not in r.headers:
                            raise FetchError("redirect_error", "Редирект без Location")
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code in (404, 410):
                        return {"url": current, "http_status": r.status_code, "method": "http", "missing": True}
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.cfg.max_page_bytes:
                            raise FetchError("size_limit", "Страница превышает лимит размера")
                    typ = r.headers.get("content-type", "").lower()
                    if data[:5] == b"%PDF-" or "application/pdf" in typ:
                        try:
                            extracted = await asyncio.to_thread(pdf_extract, bytes(data), current)
                        except Exception:
                            raise FetchError("pdf_extraction", "PDF не удалось прочитать: шифрование, скан или повреждённый файл")
                        return {"url": current, "http_status": r.status_code, "method": "http_pdf", "extracted": extracted}
                    encoding = r.encoding or "utf-8"
                    html = bytes(data).decode(encoding, errors="replace")
                    problem = access_problem(html, r.status_code)
                    if problem:
                        code, msg = problem
                        retry = r.headers.get("retry-after", "0")
                        raise FetchError(code, msg, code in ("rate_limit", "server_error", "js_required"), min(float(retry), 3600) if retry.isdigit() else 0, http_status=r.status_code)
                    if r.status_code >= 400:
                        raise FetchError("http_error", f"HTTP {r.status_code}", http_status=r.status_code)
                    if "html" not in typ and len(data) < 10:
                        raise FetchError("empty_page", "Пустой ответ", True)
                    return {"url": current, "http_status": r.status_code, "method": "http", "extracted": html_extract(html, current)}
            except httpx.TimeoutException:
                raise FetchError("timeout", "Истекло время HTTP-запроса", True)
            except httpx.ConnectError:
                raise FetchError("connection_error", "Не удалось подключиться (включая TLS)", True)
            except httpx.HTTPError:
                raise FetchError("network_error", "Ошибка передачи HTTP", True)
        raise FetchError("redirect_loop", "Слишком много перенаправлений")

    async def get_browser(self):
        async with self.browser_lock:
            if not self.browser or not self.browser.is_connected():
                if self.pw:
                    try:
                        await self.pw.stop()
                    except RuntimeError:
                        pass
                from playwright.async_api import async_playwright
                self.pw = await async_playwright().start()
                try:
                    self.browser = await self.pw.chromium.launch(headless=True)
                except Exception:
                    raise FetchError("browser_unavailable", "Chromium недоступен; выполните playwright install chromium")
        return self.browser

    async def rendered(self, url, task_id):
        domain = await validate_url(url)
        await self.pace(domain)
        browser = await self.get_browser()
        state = self.session_path(domain)
        context = await browser.new_context(storage_state=str(state) if state.exists() else None,
                                            accept_downloads=False, service_workers="block")
        page = await context.new_page()
        bad_navigation = []

        async def route_request(route):
            req = route.request
            if req.resource_type in ("image", "font", "media"):
                await route.abort()
                return
            try:
                await validate_url(req.url)
                if req.method not in ("GET", "HEAD"):
                    # Research agent does not submit forms, register or log in.
                    await route.abort()
                    return
                await route.continue_()
            except (FetchError, ValueError):
                if req.is_navigation_request() and req.frame == page.main_frame:
                    bad_navigation.append(req.url)
                await route.abort()

        await context.route("**/*", route_request)
        try:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.cfg.request_timeout*1000))
            except Exception:
                # Some sites keep subresources loading after the useful document arrives.
                if len((await page.locator("body").inner_text(timeout=1000)).strip()) < 300:
                    raise
                response = None
            await page.wait_for_timeout(900)
            if bad_navigation:
                raise FetchError("unsafe_redirect", "Браузер заблокировал переход во внутреннюю сеть")
            html = await page.content()
            code = response.status if response else 200
            problem = access_problem(html, code)
            if problem:
                artifact = f"{task_id}.png"
                await page.screenshot(path=str(self.cfg.data_dir / "evidence" / artifact))
                # Save public text for diagnosis; no cookies or request headers.
                (self.cfg.data_dir / "evidence" / f"{task_id}.txt").write_text((await page.locator("body").inner_text())[:12000])
                kind, msg = problem
                raise FetchError(kind, msg, kind in ("rate_limit", "server_error", "js_required"), artifact=artifact, http_status=code)
            if code in (404, 410):
                return {"url": page.url, "http_status": code, "method": "playwright", "missing": True}
            extracted = html_extract(html, page.url)
            for frame in page.frames[1:5]:
                try:
                    txt = (await frame.locator("body").inner_text(timeout=1500))[:2000]
                    if re_audio(txt):
                        extracted["evidence"].append({"kind": "audio_playlist", "quote": txt[:200], "url": frame.url, "verified": "rendered_dom"})
                        extracted["format"] = "audiobook"
                except Exception:
                    pass
            return {"url": page.url, "http_status": code, "method": "playwright", "extracted": extracted}
        except FetchError:
            raise
        except Exception as e:
            from playwright.async_api import TimeoutError as PWTimeout
            raise FetchError("browser_timeout" if isinstance(e, PWTimeout) else "browser_error", "Браузер не завершил загрузку страницы", True)
        finally:
            await context.close()

    async def fetch(self, url, task_id, force_browser=False, fresh=False):
        domain = await validate_url(url)
        state = self.session_path(domain)
        session_version = str(state.stat().st_mtime_ns) if state.exists() else "anonymous"
        key = hashlib.sha256(("extractor-v2:" + url + session_version + str(force_browser)).encode()).hexdigest()
        cached = None if fresh else self.store.cache_get("page_cache", key, self.cfg.cache_ttl_seconds)
        if cached:
            return {**cached, "cached": True}
        if force_browser:
            result = await self.rendered(url, task_id)
        else:
            try:
                result = await self.http(url)
            except FetchError as e:
                if self.cfg.browser_enabled and e.code in ("captcha", "blocked", "empty_page", "auth_required", "timeout", "js_required"):
                    result = await self.rendered(url, task_id)
                else:
                    raise
            if self.cfg.browser_enabled and not result.get("missing") and len(result.get("extracted", {}).get("text", "")) < 160:
                result = await self.rendered(url, task_id)
        self.store.cache_put("page_cache", key, result)
        return result


def re_audio(text):
    import re
    return bool(re.search(r"(?:глава|chapter|cvetok)\s*[\(]?\d+", text, re.I))
