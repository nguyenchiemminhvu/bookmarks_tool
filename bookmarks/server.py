"""Local HTTP server: JSON API under /api, static single-page UI under /."""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .checker import Checker, fetch_title
from .credentials import CredentialStore
from .errors import DuplicateURL, NotFound
from .importer import parse_bookmarks_html
from .store import Store, normalize_url

STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY = 64 * 1024 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second instance bind the same port silently.
    allow_reuse_address = sys.platform != "win32"


def _str(data: dict, key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def _ids(data: dict) -> list[int]:
    value = data.get("ids")
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        raise ValueError("ids must be a list of integers")
    return value


def _link_fields(data: dict) -> dict:
    return {
        "url": _str(data, "url"),
        "title": _str(data, "title"),
        "notes": _str(data, "notes"),
        "tags": _str_list(data, "tags"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Bookmarks/1.0"
    store: Store
    credentials: CredentialStore
    checker: Checker

    ROUTES = [
        ("GET", r"/", "get_index"),
        ("GET", r"/static/([\w.-]+)", "get_static"),
        ("GET", r"/api/links", "get_links"),
        ("POST", r"/api/links", "post_link"),
        ("PUT", r"/api/links/(\d+)", "put_link"),
        ("DELETE", r"/api/links/(\d+)", "delete_link"),
        ("POST", r"/api/links/(\d+)/check", "post_link_check"),
        ("POST", r"/api/bulk", "post_bulk"),
        ("POST", r"/api/tags/rename", "post_tag_rename"),
        ("DELETE", r"/api/tags/([^/]+)", "delete_tag"),
        ("GET", r"/api/check", "get_check"),
        ("POST", r"/api/check", "post_check"),
        ("GET", r"/api/title", "get_title"),
        ("POST", r"/api/import", "post_import"),
        ("GET", r"/api/export", "get_export"),
        ("GET", r"/api/credentials", "get_credentials"),
        ("POST", r"/api/credentials", "post_credential"),
        ("PUT", r"/api/credentials/(\w+)", "put_credential"),
        ("DELETE", r"/api/credentials/(\w+)", "delete_credential"),
        ("POST", r"/api/credentials/test", "post_credential_test"),
    ]
    _COMPILED = [(method, re.compile(pattern), name) for method, pattern, name in ROUTES]

    def log_message(self, format: str, *args) -> None:  # silence per-request logging
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # ---- plumbing ----------------------------------------------------------

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _dispatch(self, method: str) -> None:
        try:
            # Host check blocks DNS rebinding; Origin check blocks cross-site writes.
            if self.headers.get("Host") not in self._allowed_hosts():
                raise ApiError(403, "invalid Host header")
            origin = self.headers.get("Origin")
            if method != "GET" and origin and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
                raise ApiError(403, "cross-origin request rejected")
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            for route_method, pattern, name in self._COMPILED:
                match = pattern.fullmatch(parts.path)
                if match and route_method == method:
                    getattr(self, name)(query, *match.groups())
                    return
            raise ApiError(404, "not found")
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except NotFound as exc:
            self._json({"error": str(exc)}, 404)
        except DuplicateURL as exc:
            self._json({"error": str(exc)}, 409)
        except (ValueError, KeyError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"internal error: {exc}"}, 500)

    def _body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON object expected")
        return data

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        path = STATIC_DIR / name
        if path.suffix not in CONTENT_TYPES or not path.is_file():
            raise ApiError(404, "not found")
        self._send(200, path.read_bytes(), CONTENT_TYPES[path.suffix], {"Content-Security-Policy": CSP})

    # ---- routes ------------------------------------------------------------

    def get_index(self, query) -> None:
        self._static("index.html")

    def get_static(self, query, name: str) -> None:
        self._static(name)

    def get_links(self, query) -> None:
        self._json(self.store.list_links())

    def post_link(self, query) -> None:
        self._json(self.store.add_link(**_link_fields(self._body())), 201)

    def put_link(self, query, link_id: str) -> None:
        self._json(self.store.update_link(int(link_id), **_link_fields(self._body())))

    def delete_link(self, query, link_id: str) -> None:
        self.store.delete_link(int(link_id))
        self._json({"ok": True})

    def post_link_check(self, query, link_id: str) -> None:
        link = self.store.get_link(int(link_id))
        result = self.checker.check_one(link["url"])
        self.store.set_check_result(link["id"], result.status, result.code, result.error, int(time.time()))
        self._json(self.store.get_link(link["id"]))

    def post_bulk(self, query) -> None:
        data = self._body()
        ids = _ids(data)
        if data.get("delete") is True:
            self.store.delete_links(ids)
        else:
            self.store.retag(ids, add=_str_list(data, "add_tags"), remove=_str_list(data, "remove_tags"))
        self._json({"ok": True})

    def post_tag_rename(self, query) -> None:
        data = self._body()
        self._json({"name": self.store.rename_tag(_str(data, "from"), _str(data, "to"))})

    def delete_tag(self, query, name: str) -> None:
        self.store.delete_tag(unquote(name))
        self._json({"ok": True})

    def get_check(self, query) -> None:
        self._json(self.checker.status())

    def post_check(self, query) -> None:
        data = self._body()
        items = self.store.link_urls(None if data.get("ids") is None else _ids(data))
        if not self.checker.start(items):
            raise ApiError(409, "a link check is already running")
        self._json(self.checker.status(), 202)

    def get_title(self, query) -> None:
        url = normalize_url(query.get("url", [""])[0])
        self._json({"title": fetch_title(url, headers_for=self.credentials.headers_for)})

    def post_import(self, query) -> None:
        data = self._body()
        if "backup" in data:
            self._json(self.store.import_backup(data["backup"]))
        else:
            self._json(self.store.import_links(parse_bookmarks_html(_str(data, "html"))))

    def get_export(self, query) -> None:
        body = json.dumps(self.store.export_data(), indent=2, ensure_ascii=False).encode("utf-8")
        filename = time.strftime("bookmarks-%Y%m%d.json")
        self._send(200, body, "application/json; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{filename}"'})

    def get_credentials(self, query) -> None:
        self._json(self.credentials.list())

    def post_credential(self, query) -> None:
        self._json(self.credentials.add(self._body()), 201)

    def put_credential(self, query, cred_id: str) -> None:
        self._json(self.credentials.update(cred_id, self._body()))

    def delete_credential(self, query, cred_id: str) -> None:
        self.credentials.delete(cred_id)
        self._json({"ok": True})

    def post_credential_test(self, query) -> None:
        url = normalize_url(_str(self._body(), "url"))
        result = self.checker.check_one(url)
        self._json({"url": url, "status": result.status, "code": result.code, "error": result.error,
                    "credential": self.credentials.find(url)})


def make_server(store: Store, credentials: CredentialStore, port: int) -> ThreadingHTTPServer:
    """Create (but do not start) a server bound to 127.0.0.1:``port``; port 0 picks a free one."""
    handler = type("BoundHandler", (Handler,), {
        "store": store,
        "credentials": credentials,
        "checker": Checker(store, credentials.headers_for),
    })
    return _Server(("127.0.0.1", port), handler)
