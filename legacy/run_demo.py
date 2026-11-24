"""启动 C 服务、导入合成三句作业、托管 B 的练习页。"""

import argparse
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from backend import DEFAULT_DATA_DIR, ROOT
from backend.__main__ import port_number
from backend.course import CourseError, json_text, normalize_grant, read_json
from backend.importer import Inspector, import_course
from backend.store import Store
from backend.web import make_server, serve


def is_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_for_c(process, port):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OSError(f"C 服务启动失败（退出码 {process.returncode}）")
        if is_listening(port):
            return
        time.sleep(0.05)
    raise OSError("等待 C 服务启动超时")


def stop_c(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description="戏韵 AI 学伴 · 本机三句演示")
    parser.add_argument("--port", type=port_number, default=8000)
    parser.add_argument("--ai-port", type=port_number, default=8300)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--platform", action="store_true", help="同时提供班级工作台")
    args = parser.parse_args(argv)
    try:
        store = Store(args.data_dir)
        token = secrets.token_urlsafe(32)
        inspector = Inspector(args.ai_port, token)
        with make_server(store, args.port, "c1", platform=args.platform, inspector=inspector) as server:
            if is_listening(args.ai_port):
                raise OSError(f"C 端口 {args.ai_port} 已占用，请指定其他 --ai-port")
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", "-m", "ai_backend",
                 "--host", "127.0.0.1", "--port", str(args.ai_port)],
                cwd=ROOT, env=dict(os.environ, AI_BACKEND_TOKEN=token),
            )
            try:
                wait_for_c(process, args.ai_port)
                store.register_grant(normalize_grant(read_json(ROOT / "contracts" / "demo-grant.json")))
                result = import_course(
                    store, inspector, read_json(ROOT / "contracts" / "demo-course.json"),
                    ROOT / "ai_backend" / "samples" / "demo_12s.wav",
                )
                print(json_text(result), flush=True)
                if result["status"] != "published":
                    print("此作业已失效。可用 --data-dir 指定新目录来建立一套演示数据。", flush=True)
                if args.platform:
                    print(f"班级工作台：http://127.0.0.1:{args.port}/workspace/", flush=True)
                serve(server)
            finally:
                stop_c(process)
    except KeyboardInterrupt:
        return 0
    except CourseError as error:
        print(json_text(error.body()), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"无法启动演示：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
