"""Bounded internal HTTP adapter; no database or publication state."""
from __future__ import annotations
from email.message import Message
import hmac
import json
import math
import socket
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .inspection import MAX_FILE_BYTES, InspectError, decode_metadata, inspect
from .multipart import parse_multipart

INSPECT_PATH = "/internal/v1/inspect"
MAX_BODY_BYTES = MAX_FILE_BYTES + 1024 * 1024
READ_TIMEOUT_SECONDS = 10.0


class InspectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ai-inspect/1"
    expected_token = ""
    read_timeout = READ_TIMEOUT_SECONDS

    def setup(self):
        super().setup()
        self.connection.settimeout(self.read_timeout)

    def _send_json(self, status, body, close=True):
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (OSError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        rendered = (fmt % args).encode("unicode_escape").decode("ascii")
        sys.stderr.write("[ai-inspect] " + rendered + "\n")

    def _drain_rejected_body(self):
        """Respond first, then briefly drain uploads to avoid Windows client resets."""
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                return
            remaining = int(lengths[0])
            if remaining > MAX_BODY_BYTES:
                return
            deadline = time.monotonic() + 0.25
            while remaining and time.monotonic() < deadline:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError, TimeoutError):
            pass

    def _content_length(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise InspectError(400, "INVALID_INPUT", "请使用 Content-Length，不支持 Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise InspectError(400, "INVALID_INPUT", "需要唯一且合法的 Content-Length")
        try:
            length = int(lengths[0])
        except ValueError as error:
            raise InspectError(400, "INVALID_INPUT", "Content-Length 无效") from error
        if length <= 0:
            raise InspectError(400, "INVALID_INPUT", "请求体不能为空")
        if length > MAX_BODY_BYTES:
            raise InspectError(413, "TOO_LARGE", "请求超过大小上限", "audio")
        return length

    def _boundary(self):
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or len(types[0]) > 1024:
            raise InspectError(400, "INVALID_INPUT", "需要唯一的合法 Content-Type")
        message = Message()
        message["Content-Type"] = types[0]
        params = message.get_params() or []
        if (message.get_content_type() != "multipart/form-data"
                or sum(key.lower() == "boundary" for key, _ in params) != 1):
            raise InspectError(400, "INVALID_INPUT", "请求必须是带唯一 boundary 的 multipart/form-data")
        boundary = message.get_boundary()
        try:
            if not boundary:
                raise ValueError("missing boundary")
            return boundary.encode("ascii")
        except (UnicodeError, ValueError, AttributeError) as error:
            raise InspectError(400, "INVALID_INPUT", "multipart boundary 无效") from error

    def do_POST(self):
        if self.path != INSPECT_PATH:
            self.do_GET()
            self._drain_rejected_body()
            return
        auth = self.headers.get_all("Authorization", [])
        expected = ("Bearer " + self.expected_token).encode("utf-8")
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), expected):
            self._send_json(401, InspectError(401, "INVALID_TOKEN", "内部令牌缺失或不正确").to_body(None))
            self._drain_rejected_body()
            return
        request_id = None
        body_read = False
        try:
            length = self._content_length()
            boundary = self._boundary()
            deadline = time.monotonic() + self.read_timeout
            chunks = bytearray()
            while len(chunks) < length:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError()
                self.connection.settimeout(remaining_time)
                chunk = self.rfile.read1(min(65536, length - len(chunks)))
                if not chunk:
                    raise InspectError(400, "INVALID_INPUT", "请求体截断")
                chunks.extend(chunk)
            body_read = True
            parts = parse_multipart(bytes(chunks), boundary)
            metadata = decode_metadata(parts["metadata"])
            if isinstance(metadata, dict) and isinstance(metadata.get("request_id"), str):
                request_id = metadata["request_id"]
            result = inspect(parts["audio"], metadata)
        except InspectError as error:
            self._send_json(error.status, error.to_body(request_id))
            if not body_read:
                self._drain_rejected_body()
        except (TimeoutError, ConnectionError, OSError):
            self._send_json(400, InspectError(400, "INVALID_INPUT", "读取请求体超时或连接中断").to_body(request_id))
        except Exception:
            traceback.print_exc()
            self._send_json(503, InspectError(503, "ANALYSIS_UNAVAILABLE", "分析服务内部故障，可稍后重试").to_body(request_id))
        else:
            self._send_json(200, result)

    def do_GET(self):
        self._send_json(404, {"request_id": None, "code": "INVALID_INPUT",
                              "message": f"仅支持 POST {INSPECT_PATH}"})


def make_server(host, port, token, *, read_timeout=READ_TIMEOUT_SECONDS):
    if (not isinstance(token, str) or not token or not token.isascii()
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ValueError("内部令牌必须是非空、无空白的可打印 ASCII 字符串")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须为 0 到 65535 的整数；命令行启动请使用 1 到 65535")
    if not isinstance(read_timeout, (int, float)) or not math.isfinite(read_timeout) or read_timeout <= 0:
        raise ValueError("请求读取超时必须是有限正数")
    handler = type("BoundInspectHandler", (InspectHandler,),
                   {"expected_token": token, "read_timeout": read_timeout})
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return Server((host, port), handler)
