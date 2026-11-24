"""可重放请求客户端：python -m ai_backend.replay

向运行中的服务发送一次真实 multipart HTTP 请求，打印状态码与响应 JSON。
供 C 自测，也供 A 从自己的进程复用（send_inspect 是纯标准库函数）。

示例（仓库根目录执行）：
  python -m ai_backend.replay --token dev-token-123 \
      --audio ai_backend/samples/demo_12s.wav \
      --metadata ai_backend/samples/success.json
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .multipart import build_multipart
from .server import INSPECT_PATH
from . import PROCESSOR_VERSION


def validate_success(result: dict, metadata: dict, *, expected_version: str = PROCESSOR_VERSION) -> None:
    """Caller-side guard against stale IDs/revisions or unexpected processor output."""
    if not isinstance(result, dict):
        raise ValueError("成功结果必须是 JSON 对象")
    expected = {"request_id": metadata["request_id"], "revision_id": metadata["revision_id"],
                "processor_version": expected_version, "phrase_count": len(metadata["phrases"])}
    for field, value in expected.items():
        if result.get(field) != value:
            raise ValueError(f"返回 {field} 与本次请求或配置不一致")
    for field in ("duration_ms", "sample_rate_hz", "channels", "phrase_count"):
        if type(result.get(field)) is not int or result[field] <= 0:
            raise ValueError(f"返回 {field} 必须是正整数")
    if result["channels"] not in (1, 2):
        raise ValueError("返回声道数不受支持")


def send_inspect(base_url: str, token: str, audio: bytes, metadata_text: bytes,
                 timeout: float = 30.0) -> tuple[int, dict]:
    """发送一次 inspect 请求，返回 (HTTP 状态码, 响应 JSON)。超时按契约默认 30 秒。"""
    body, boundary = build_multipart(
        {"audio": audio, "metadata": metadata_text}, filenames={"audio": "demo.wav"})
    request = urllib.request.Request(
        base_url.rstrip("/") + INSPECT_PATH,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    open_request = urllib.request.urlopen
    if urlsplit(base_url).hostname in ("127.0.0.1", "localhost", "::1"):
        # A local C request must never send its bearer token or WAV to an HTTP proxy.
        open_request = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_request(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        with err:
            return err.code, json.loads(err.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_backend.replay", description="重放一次 inspect 请求")
    parser.add_argument("--url", default="http://127.0.0.1:8300", help="服务基地址")
    parser.add_argument("--token", required=True, help="内部 Bearer 令牌")
    parser.add_argument("--audio", required=True, help="WAV 文件路径")
    parser.add_argument("--metadata", required=True, help="metadata JSON 文件路径")
    parser.add_argument("--timeout", type=float, default=30.0, help="超时秒数，默认 30")
    args = parser.parse_args(argv)

    status, payload = send_inspect(
        args.url, args.token,
        Path(args.audio).read_bytes(), Path(args.metadata).read_bytes(),
        timeout=args.timeout,
    )
    print(f"HTTP {status}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
