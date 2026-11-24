"""本机操作员命令，入口为 python -m backend。"""

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from . import DEFAULT_DATA_DIR
from .course import CourseError, json_text, normalize_grant, read_json
from .importer import Inspector, import_course
from .platform.access import ROLES, create_account
from .store import Store
from .web import make_server, serve


def port_number(raw):
    port = int(raw)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口须在 1–65535 之间")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend", description="戏韵 AI 学伴 · 本机作业服务")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="私有作业库目录")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("serve", help="启动练习页和取流服务")
    web.add_argument("--port", type=port_number, default=8000)
    web.add_argument("--default-course", default="c1", help="打开根地址时进入的作业")
    web.add_argument("--platform", action="store_true", help="启用本机班级工作台")
    web.add_argument("--ai-port", type=port_number, default=os.environ.get("AI_BACKEND_PORT", "8300"))
    account = commands.add_parser("create-account", help="创建工作台账号，密码由终端交互读取")
    account.add_argument("--id", required=True)
    account.add_argument("--name", required=True)
    account.add_argument("--role", choices=ROLES, required=True)
    grant = commands.add_parser("register-grant", help="登记许可台账")
    grant.add_argument("--grant", type=Path, required=True)
    upload = commands.add_parser("import-course", help="检查并发布复核过的作业")
    upload.add_argument("--manifest", type=Path, required=True)
    upload.add_argument("--audio", type=Path, required=True)
    upload.add_argument("--ai-port", type=port_number, default=os.environ.get("AI_BACKEND_PORT", "8300"))
    withdraw = commands.add_parser("withdraw", help="撤回已发布作业")
    withdraw.add_argument("--course", required=True)
    args = parser.parse_args(argv)

    try:
        store = Store(args.data_dir)
        if args.command == "serve":
            inspector = Inspector(args.ai_port, os.environ.get("AI_BACKEND_TOKEN", ""))
            with make_server(store, args.port, args.default_course, platform=args.platform, inspector=inspector) as server:
                serve(server)
            return 0
        if args.command == "create-account":
            password = getpass.getpass("密码：")
            if password != getpass.getpass("再输一次："):
                raise CourseError(400, "PASSWORD_MISMATCH", "两次输入的密码不一致")
            create_account(store, args.id, args.name, args.role, password)
            result = {"account_id": args.id, "role": args.role, "status": "created"}
        elif args.command == "register-grant":
            record = normalize_grant(read_json(args.grant))
            store.register_grant(record)
            result = {"grant_ref": record["grant_ref"], "status": "registered"}
        elif args.command == "import-course":
            inspector = Inspector(args.ai_port, os.environ.get("AI_BACKEND_TOKEN", ""))
            result = import_course(store, inspector, read_json(args.manifest), args.audio)
        else:
            store.withdraw(args.course)
            result = {"course_id": args.course, "status": "withdrawn"}
        print(json_text(result))
        return 0
    except CourseError as error:
        print(json_text(error.body()), file=sys.stderr)
    except (OSError, sqlite3.Error) as error:
        print(json_text({"code": "LOCAL_ERROR", "message": str(error)}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
