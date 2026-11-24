"""Local frontend service. Run: python frontend_server.py"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
CLIENT_ASSETS = ('personal/index.html', 'personal/styles.css', 'personal/app.js', 'personal/media.js',
                 'personal/storage.js', 'personal/knowledge.js', 'personal/analysis.js', 'personal/analysis-worker.js',
                 'personal-sw.js', 'personal/manifest.webmanifest', 'personal/icon-192.png', 'personal/icon-512.png',
                 'styles.css', 'assets/rehearsal.webp', 'src/course.js', 'src/audio.js')
WEB_FILES = CLIENT_ASSETS + ('index.html', 'src/app.js', 'src/view.js', 'src/progress.js', '示例数据/三句作业.json')
TEST_FILES = ("tests/browser.html", "tests/logic-tests.js")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


class FrontendHandler(BaseHTTPRequestHandler):
    """One HTTP adapter for the small, explicitly allowed static frontend."""
    server_version = "XiyunFrontend/0.1"

    def _reply(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        if self.command not in ("GET", "HEAD"):
            self._reply(405, b"Only GET and HEAD are supported", headers={"Allow": "GET, HEAD"})
            return
        path = urlsplit(self.path).path
        name = "personal/index.html" if path == "/" else unquote(path[1:])
        if name not in WEB_FILES + TEST_FILES or ROOT not in (ROOT / name).resolve().parents:
            body = json.dumps({"code": "NOT_FOUND", "message": "找不到本机客户端资源。"}, ensure_ascii=False).encode("utf-8")
            self._reply(404, body, CONTENT_TYPES[".json"])
            return
        try:
            body = (ROOT / name).read_bytes()
        except OSError:
            self._reply(500, b"Unable to read frontend file")
            return
        self._reply(200, body, CONTENT_TYPES[Path(name).suffix])

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def port_number(value):
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="戏韵本地前端服务（Python）")
    parser.add_argument("--port", type=port_number, default=os.environ.get("PORT", "5173"))
    args = parser.parse_args(argv)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), FrontendHandler)
    except OSError as error:
        print(f"无法启动端口 {args.port}：{error}\n如果端口已被占用，可运行 python frontend_server.py --port 5174", file=sys.stderr)
        return 1
    with server:
        address = f"http://127.0.0.1:{server.server_port}"
        print(f"戏韵前端服务：{address}/\n浏览器逻辑检查：{address}/tests/browser.html\n按 Ctrl+C 停止。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n前端服务已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
