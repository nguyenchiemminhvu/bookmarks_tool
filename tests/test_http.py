import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bookmarks.checker import check_url
from bookmarks.credentials import CredentialStore
from bookmarks.server import make_server
from bookmarks.store import Store


class _Target(BaseHTTPRequestHandler):
    """Fake intranet site."""

    def log_message(self, *args):
        pass

    def _reply(self, code, location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        authorized = self.headers.get("Authorization") == "Bearer good"
        routes = {
            "/ok": 200,
            "/missing": 404,
            "/secret": 200 if authorized else 401,
            "/nohead": 405 if head else 200,
            "/server-error": 500,
        }
        if self.path == "/sso-redirect":
            self._reply(302, "/login.jsp?os_destination=/x")
        elif self.path == "/login.jsp?os_destination=/x":
            self._reply(200)
        elif self.path == "/to-secret":
            self._reply(302, "/secret")
        elif self.path == "/to-other-host":
            # Redirect to a different host name of the same server.
            port = self.server.server_address[1]
            self._reply(302, f"http://localhost:{port}/echo-auth")
        elif self.path == "/echo-auth":
            self._reply(401 if self.headers.get("Authorization") else 200)
        else:
            self._reply(routes.get(self.path, 404))


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Target))
        cls.base = f"http://127.0.0.1:{cls.target.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.target.shutdown()
        cls.target.server_close()

    def status(self, path, headers_for=None):
        return check_url(self.base + path, timeout=5, headers_for=headers_for).status

    def test_status_classification(self):
        self.assertEqual(self.status("/ok"), "ok")
        self.assertEqual(self.status("/missing"), "broken")
        self.assertEqual(self.status("/secret"), "auth")
        self.assertEqual(self.status("/nohead"), "ok")  # HEAD rejected, GET retried
        self.assertEqual(self.status("/server-error"), "error")
        self.assertEqual(self.status("/sso-redirect"), "auth")
        self.assertEqual(check_url("javascript:alert(1)").status, "skipped")

    def test_unreachable(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertEqual(check_url(f"http://127.0.0.1:{port}/", timeout=5).status, "unreachable")

    def test_credentials_follow_same_host_redirect_but_not_other_hosts(self):
        def headers_for(url):
            return {"Authorization": "Bearer good"} if url.startswith(self.base) else {}

        self.assertEqual(self.status("/secret", headers_for), "ok")
        self.assertEqual(self.status("/to-secret", headers_for), "ok")
        # /echo-auth answers 401 if any Authorization header reaches the other host.
        self.assertEqual(self.status("/to-other-host", headers_for), "ok")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.server = _serve(make_server(Store(data / "b.db"), CredentialStore(data / "c.json"), 0))
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        all_headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            all_headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        payload = json.loads(response.read() or b"null")
        conn.close()
        return response.status, payload

    def test_link_crud_roundtrip(self):
        status, link = self.request("POST", "/api/links",
                                    {"url": "https://a.example.com/", "title": "A", "tags": ["Jira", "BMW ICON"]})
        self.assertEqual(status, 201)
        self.assertEqual(link["tags"], ["bmw-icon", "jira"])
        self.assertEqual(self.request("POST", "/api/links", {"url": "https://a.example.com/"})[0], 409)
        status, updated = self.request("PUT", f"/api/links/{link['id']}",
                                       {"url": "https://a.example.com/", "title": "B", "tags": []})
        self.assertEqual((status, updated["title"], updated["tags"]), (200, "B", []))
        self.assertEqual(self.request("DELETE", f"/api/links/{link['id']}")[0], 200)
        self.assertEqual(self.request("GET", "/api/links"), (200, []))

    def test_chrome_export_download(self):
        self.request("POST", "/api/links", {"url": "https://a.example.com/", "tags": ["jira"]})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/export?format=chrome", headers={"Host": f"127.0.0.1:{self.port}"})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn("<H3>jira</H3>", body)

    def test_cross_site_and_rebinding_requests_rejected(self):
        body = {"url": "https://evil.example.com/"}
        self.assertEqual(self.request("POST", "/api/links", body, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/links", body, {"Origin": "https://evil.example.com"})[0], 403)
        self.assertEqual(self.request("GET", "/api/credentials", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.request("GET", "/api/links"), (200, []))

    def test_credential_secret_never_returned(self):
        status, cred = self.request("POST", "/api/credentials",
                                    {"host": "jira.example.com", "type": "bearer", "secret": "s3cret"})
        self.assertEqual(status, 201)
        _, listed = self.request("GET", "/api/credentials")
        self.assertNotIn("s3cret", json.dumps([cred, listed]))


if __name__ == "__main__":
    unittest.main()
