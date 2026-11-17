"""HTTP liveness checks and page-title fetching."""
from __future__ import annotations

import html
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .store import Store

OK = "ok"                    # 2xx/3xx
AUTH = "auth"                # 401/403/407 or redirected to a login page
BROKEN = "broken"            # 404/410: page is gone
UNREACHABLE = "unreachable"  # DNS failure, refused connection, timeout
ERROR = "error"              # any other HTTP error or protocol failure
SKIPPED = "skipped"          # not an http(s) URL

HeadersFor = Callable[[str], "dict[str, str]"]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bookmarks-dashboard/1.0",
    "Accept": "*/*",
}
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LOGIN_RE = re.compile(r"log-?in|sign-?in|/sso/|saml|adfs|oauth|openid|/auth/realms/|j_security_check",
                       re.IGNORECASE)


def _apply_auth(request: urllib.request.Request, headers_for: HeadersFor) -> None:
    # Unredirected headers are not copied by urllib onto redirect requests;
    # _AuthRedirectHandler re-resolves them for the new URL instead.
    request.headers_for = headers_for
    for name, value in headers_for(request.full_url).items():
        request.add_unredirected_header(name, value)


class _AuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        headers_for = getattr(req, "headers_for", None)
        if new is not None and headers_for is not None:
            _apply_auth(new, headers_for)
        return new


def _build_opener() -> urllib.request.OpenerDirector:
    # Liveness only: accept the self-signed certificates common on intranet tools.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _AuthRedirectHandler)


_OPENER = _build_opener()


def _request(url: str, method: str, headers_for: HeadersFor | None) -> urllib.request.Request:
    request = urllib.request.Request(url, method=method, headers=_HEADERS)
    if headers_for is not None:
        _apply_auth(request, headers_for)
    return request


@dataclass(frozen=True)
class CheckResult:
    status: str
    code: int | None = None
    error: str | None = None


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _is_login_url(url: str) -> bool:
    parts = urlsplit(url)
    return bool(_LOGIN_RE.search(f"{parts.path}?{parts.query}"))


def classify(code: int) -> str:
    if code < 400:
        return OK
    if code in (401, 403, 407):
        return AUTH
    if code in (404, 410):
        return BROKEN
    return ERROR


def check_url(url: str, timeout: float = 10.0, headers_for: HeadersFor | None = None) -> CheckResult:
    if not _is_http(url):
        return CheckResult(SKIPPED, error="not an http(s) URL")
    code, final_url = 0, url
    # Some servers reject or mishandle HEAD, so any HEAD failure status is retried with GET.
    for method in ("HEAD", "GET"):
        try:
            with _OPENER.open(_request(url, method, headers_for), timeout=timeout) as response:
                code, final_url = response.status, response.geturl()
        except urllib.error.HTTPError as exc:
            code, final_url = exc.code, exc.geturl() or url
            exc.close()
        except urllib.error.URLError as exc:
            return CheckResult(UNREACHABLE, error=str(exc.reason))
        except OSError as exc:
            return CheckResult(UNREACHABLE, error=str(exc) or type(exc).__name__)
        except Exception as exc:  # malformed URL, protocol errors
            return CheckResult(ERROR, error=f"{type(exc).__name__}: {exc}")
        if code < 400:
            break
    if code < 400 and final_url != url and _is_login_url(final_url) and not _is_login_url(url):
        return CheckResult(AUTH, code, f"redirected to login page {final_url}")
    return CheckResult(classify(code), code)


def fetch_title(url: str, timeout: float = 8.0, headers_for: HeadersFor | None = None) -> str:
    """Return the page <title>, or "" if it cannot be fetched."""
    if not _is_http(url):
        return ""
    try:
        with _OPENER.open(_request(url, "GET", headers_for), timeout=timeout) as response:
            raw = response.read(512 * 1024)
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception:
        return ""
    match = _TITLE_RE.search(raw)
    if not match:
        return ""
    try:
        text = match.group(1).decode(charset, "replace")
    except LookupError:
        text = match.group(1).decode("utf-8", "replace")
    return " ".join(html.unescape(text).split())


class Checker:
    """Runs one background check job at a time and records results in the store."""

    def __init__(self, store: Store, headers_for: HeadersFor | None = None,
                 workers: int = 16, timeout: float = 10.0) -> None:
        self._store = store
        self._headers_for = headers_for
        self._workers = workers
        self._timeout = timeout
        self._lock = threading.Lock()
        self._running = False
        self._total = 0
        self._done = 0

    def status(self) -> dict:
        with self._lock:
            return {"running": self._running, "total": self._total, "done": self._done}

    def check_one(self, url: str) -> CheckResult:
        return check_url(url, self._timeout, self._headers_for)

    def start(self, items: list[tuple[int, str]]) -> bool:
        """Start checking ``(link_id, url)`` pairs; False if a job is already running."""
        with self._lock:
            if self._running:
                return False
            self._running, self._total, self._done = True, len(items), 0
        threading.Thread(target=self._run, args=(items,), daemon=True).start()
        return True

    def _run(self, items: list[tuple[int, str]]) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = {pool.submit(self.check_one, url): link_id for link_id, url in items}
                for future in as_completed(futures):
                    result = future.result()
                    self._store.set_check_result(futures[future], result.status, result.code,
                                                 result.error, int(time.time()))
                    with self._lock:
                        self._done += 1
        finally:
            with self._lock:
                self._running = False
