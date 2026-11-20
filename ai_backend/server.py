"""内部 HTTP 服务：POST /internal/v1/inspect。

仅回环访问的内部接口，Bearer 令牌鉴权。同步处理，无业务库、无发布副作用。
"""

from __future__ import annotations

import hmac
import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .inspection import MAX_FILE_BYTES, InspectError, inspect
from .multipart import parse_multipart

INSPECT_PATH = "/internal/v1/inspect"
# multipart 体 = 音频 + metadata + 边界开销，预留 1 MB 余量；音频本身仍按 100 MB 严格检查。
MAX_BODY_BYTES = MAX_FILE_BYTES + 1024 * 1024
_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?')


class InspectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ai-inspect/1"
    expected_token: str = ""  # 由 make_server 注入

    # ---------- 基础工具 ----------

    def _send_json(self, status: int, body: dict, close: bool = False) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, err: InspectError, request_id: str | None, close: bool = False) -> None:
        self._send_json(err.status, err.to_body(request_id), close=close)

    def log_message(self, fmt: str, *args) -> None:  # 精简访问日志
        sys.stderr.write("[ai-inspect] %s\n" % (fmt % args))

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return
        if 0 <= length <= MAX_BODY_BYTES:
            self.rfile.read(length)

    # ---------- 路由 ----------

    def do_POST(self) -> None:
        if self.path != INSPECT_PATH:
            self._send_json(404, {
                "request_id": None, "code": "INVALID_INPUT",
                "message": f"仅支持 POST {INSPECT_PATH}",
            })
            return

        # 1) 鉴权：不合法直接拒绝。先丢弃请求体再回包，避免客户端收到连接重置。
        auth = self.headers.get("Authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.expected_token):
            self._discard_body()
            self._send_error(InspectError(401, "INVALID_TOKEN", "内部令牌缺失或不正确"), None, close=True)
            return

        # 2) 请求体大小与类型。
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self._send_error(InspectError(400, "INVALID_INPUT", "缺少合法的 Content-Length"), None, close=True)
            return
        if length > MAX_BODY_BYTES:
            self._send_error(
                InspectError(413, "TOO_LARGE", "音频文件超过 100 MB 上限", "audio"), None, close=True)
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._send_error(
                InspectError(400, "INVALID_INPUT", "请求必须是 multipart/form-data"), None, close=True)
            return
        match = _BOUNDARY_RE.search(content_type)
        if match is None:
            self._send_error(InspectError(400, "INVALID_INPUT", "multipart 缺少 boundary"), None, close=True)
            return

        body = self.rfile.read(length)

        # 3) 解析字段。request_id 尽力回传：metadata 可解析时即使后续失败也带回。
        request_id: str | None = None
        try:
            parts = parse_multipart(body, match.group(1).encode("utf-8"))
            if "audio" not in parts:
                raise InspectError(400, "INVALID_INPUT", "缺少 audio 文件字段", "audio")
            if "metadata" not in parts:
                raise InspectError(400, "INVALID_INPUT", "缺少 metadata 文本字段", "metadata")
            try:
                metadata = json.loads(parts["metadata"].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InspectError(400, "INVALID_INPUT", f"metadata 不是合法 UTF-8 JSON（{exc}）", "metadata") from exc
            if isinstance(metadata, dict) and isinstance(metadata.get("request_id"), str):
                request_id = metadata["request_id"]

            result = inspect(parts["audio"], metadata)
            self._send_json(200, result)
            self.log_message("inspect ok request_id=%s duration_ms=%s", request_id, result["duration_ms"])
        except InspectError as err:
            self._send_error(err, request_id)
            self.log_message("inspect fail request_id=%s status=%s code=%s field=%s",
                             request_id, err.status, err.code, err.field)
        except Exception:  # noqa: BLE001 未预期故障按服务不可用回传
            traceback.print_exc()
            self._send_error(InspectError(503, "ANALYSIS_UNAVAILABLE", "分析服务内部故障，可稍后重试"), request_id)

    def do_GET(self) -> None:
        self._send_json(404, {
            "request_id": None, "code": "INVALID_INPUT",
            "message": f"仅支持 POST {INSPECT_PATH}",
        })


def make_server(host: str, port: int, token: str) -> ThreadingHTTPServer:
    handler = type("BoundInspectHandler", (InspectHandler,), {"expected_token": token})
    return ThreadingHTTPServer((host, port), handler)