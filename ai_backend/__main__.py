"""服务入口：python -m ai_backend [--host H] [--port P] [--token T]

配置优先级：命令行参数 > 环境变量（AI_BACKEND_HOST / AI_BACKEND_PORT / AI_BACKEND_TOKEN）。
令牌必须提供，否则拒绝启动。默认只绑定回环地址 127.0.0.1。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import PROCESSOR_VERSION
from .server import INSPECT_PATH, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_backend", description="戏韵 AI 学伴 · C 音频检查服务（D1）")
    parser.add_argument("--host", default=os.environ.get("AI_BACKEND_HOST", "127.0.0.1"),
                        help="监听地址，默认 127.0.0.1（契约要求仅回环访问）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_BACKEND_PORT", "8300")),
                        help="监听端口，默认 8300")
    parser.add_argument("--token", default=os.environ.get("AI_BACKEND_TOKEN", ""),
                        help="内部 Bearer 令牌；也可用环境变量 AI_BACKEND_TOKEN 提供")
    args = parser.parse_args(argv)

    if not args.token:
        print("错误：必须通过 --token 或环境变量 AI_BACKEND_TOKEN 配置内部令牌。", file=sys.stderr)
        return 2
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"警告：绑定地址 {args.host} 不是回环地址，契约要求本服务仅供 A 本机访问。", file=sys.stderr)

    server = make_server(args.host, args.port, args.token)
    print(f"[ai-inspect] 处理器版本 {PROCESSOR_VERSION}")
    print(f"[ai-inspect] 已启动：http://{args.host}:{args.port}{INSPECT_PATH}")
    print("[ai-inspect] 鉴权：Authorization: Bearer <内部令牌>；按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ai-inspect] 收到中断，正在停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())