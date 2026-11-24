"""Exercise real HTTP handlers in memory; no network permissions are needed."""

import io
import json
import re
import unittest
from http.client import HTTPResponse
from unittest.mock import patch
from urllib.parse import quote

from frontend_server import FrontendHandler, ROOT, TEST_FILES, WEB_FILES, main


class MemorySocket:
    def __init__(self, data=b""):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class QuietHandler(FrontendHandler):
    def log_message(self, *args):
        pass


def request(path, method="GET", headers=""):
    path = quote(path, safe="/?=&%")
    connection = MemorySocket(f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n{headers}\r\n".encode("ascii"))
    QuietHandler(connection, ("127.0.0.1", 12345), None)
    response = HTTPResponse(MemorySocket(bytes(connection.output)), method=method)
    response.begin()
    return response, response.read()


class FrontendServerTests(unittest.TestCase):
    def test_default_url_serves_app_without_redirect(self):
        response, body = request("/")
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Location"))
        self.assertEqual(body, (ROOT / "personal/index.html").read_bytes())

    def test_every_web_and_browser_test_asset_is_served_with_correct_type(self):
        for name in WEB_FILES + TEST_FILES:
            with self.subTest(name=name):
                response, body = request("/" + name)
                self.assertEqual(response.status, 200)
                self.assertEqual(body, (ROOT / name).read_bytes())
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                if name.endswith(".js"):
                    self.assertEqual(response.getheader("Content-Type"), "text/javascript; charset=utf-8")

    def test_query_parameters_keep_the_same_personal_entry(self):
        for path in ("/?course=c1", "/?scenario=empty"):
            with self.subTest(path=path):
                response, body = request(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(body, (ROOT / "personal/index.html").read_bytes())
                self.assertIsNone(response.getheader("Location"))

    def test_head_has_get_headers_but_no_body(self):
        response, body = request("/src/app.js", "HEAD")
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.getheader("Content-Length")), (ROOT / "src/app.js").stat().st_size)
        self.assertEqual(body, b"")

    def test_unknown_api_never_falls_back_to_demo(self):
        response, body = request("/v1/courses/c1")
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(body)["code"], "NOT_FOUND")
        self.assertNotIn(b'"phrases"', body)

    def test_private_files_and_path_traversal_are_unavailable(self):
        for path in ("/frontend_server.py", "/README.md", "/src/", "/../AGENTS.md", "/%2e%2e/AGENTS.md", "/tests/test_tools.py", "/dist/index.html"):
            with self.subTest(path=path):
                response, _ = request(path)
                self.assertEqual(response.status, 404)

    def test_non_read_requests_cannot_upload_or_change_files(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response, _ = request("/示例数据/三句作业.json", method)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.getheader("Allow"), "GET, HEAD")

    def test_port_conflict_returns_actionable_message(self):
        with patch("frontend_server.ThreadingHTTPServer", side_effect=OSError("Address already in use")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(["--port", "5173"]), 1)
            self.assertIn("--port 5174", stderr.getvalue())


class ResourceManifestTests(unittest.TestCase):
    def test_deployed_module_imports_are_all_in_the_asset_manifest(self):
        for name in WEB_FILES:
            if not name.endswith(".js"):
                continue
            text = (ROOT / name).read_text(encoding="utf-8")
            imports = re.findall(r"(?:from\s*|import\s*)['\"](\.[^'\"]+)['\"]", text)
            for imported in imports:
                with self.subTest(source=name, imported=imported):
                    target = (ROOT / name).parent.joinpath(imported).resolve().relative_to(ROOT).as_posix()
                    self.assertIn(target, WEB_FILES)


if __name__ == "__main__":
    unittest.main()
