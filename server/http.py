"""Loopback HTTP entry point for the current studio."""

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import catalog


WEB_ROOT = Path(__file__).resolve().parent.parent / "frontend"
STATIC_FILES = (
    "studio/index.html",
    "studio/app.js",
    "studio/styles.css",
    "studio/scoring.js",
    "studio/scoring-worker.js",
    "audio/features.js",
    "audio/media.js",
    "audio/recorder.js",
    "assets/icon.png",
)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
}


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "XiyunStudio/1.0"

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
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _handle(self):
        path = urlsplit(self.path).path
        if self.command not in ("GET", "HEAD"):
            if self.command == "POST" and catalog.dispatch_post(self, path):
                return
            self._reply(405, headers={"Allow": "GET, HEAD"})
            return
        if catalog.dispatch(self, path):
            return
        name = "studio/index.html" if path == "/" or catalog.PAGE_ROUTE.fullmatch(path) else unquote(path[1:])
        asset = (WEB_ROOT / name).resolve()
        if name not in STATIC_FILES or WEB_ROOT not in asset.parents:
            self._reply(404, catalog.encode({"code": "NOT_FOUND", "message": "找不到本机客户端资源。"}),
                        "application/json; charset=utf-8")
            return
        try:
            body = asset.read_bytes()
        except OSError:
            self._reply(500, b"Unable to read frontend file")
            return
        self._reply(200, body, CONTENT_TYPES[asset.suffix])

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
        raise argparse.ArgumentTypeError("端口须为整数") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口须在 0–65535 之间")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="戏韵本机练唱服务")
    parser.add_argument("--port", type=port_number, default=os.environ.get("PORT", "5173"))
    args = parser.parse_args(argv)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), StudioHandler)
    except OSError as error:
        print(f"无法启动端口 {args.port}：{error}\n可尝试 python -m server --port 5174", file=sys.stderr)
        return 1
    with server:
        print(f"戏韵练唱台：http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n练唱服务已停止。", flush=True)
    return 0
