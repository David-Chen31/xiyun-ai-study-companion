"""Replay D3 cases against real service subprocesses, including stop/restart.

python -m ai_backend.check_d3
No business backend or external network is involved.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import wave

from . import PROCESSOR_VERSION
from .inspection import MAX_METADATA_BYTES
from .replay import send_inspect, validate_success
from .server import INSPECT_PATH, MAX_BODY_BYTES

ROOT = Path(__file__).resolve().parent.parent


class ServiceProcess:
    def __init__(self, token, temporary):
        self.token, self.temporary = token, temporary
        self.process = None
        self.port = None

    def start(self):
        if self.port is None:
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                self.port = reserve.getsockname()[1]
        env = dict(os.environ, AI_BACKEND_TOKEN=self.token,
                   TMP=self.temporary, TEMP=self.temporary, TMPDIR=self.temporary)
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-B", "-m", "ai_backend",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("C 服务启动失败，请检查本机端口和 Python 环境")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("等待 C 服务启动超时")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def overlong_audio():
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(100)
        wav.writeframes(b"\0\0" * 60100)
    return target.getvalue()


def run_d3():
    samples = ROOT / "ai_backend/samples"
    audio = (samples / "demo_12s.wav").read_bytes()
    base_metadata = json.loads((samples / "success_three.json").read_text(encoding="utf-8"))
    token = secrets.token_urlsafe(24)
    cases = []

    def record(name, operation):
        started = time.perf_counter()
        try:
            details = operation()
            row = {"case": name, "passed": True, **details}
        except Exception as error:
            row = {"case": name, "passed": False, "error": str(error)}
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        cases.append(row)

    with tempfile.TemporaryDirectory(prefix="c-d3-") as temporary:
        service = ServiceProcess(token, temporary)
        before = sorted(p.relative_to(temporary).as_posix() for p in Path(temporary).rglob("*"))

        def request(status, fields=None, *, metadata=None, payload=None, credential=None, raw=None):
            data = copy.deepcopy(base_metadata if metadata is None else metadata)
            response_status, response = send_inspect(
                service.url, token if credential is None else credential,
                audio if payload is None else payload,
                json.dumps(data, ensure_ascii=False).encode("utf-8") if raw is None else raw,
                timeout=3,
            )
            if response_status != status:
                raise AssertionError(f"预期 HTTP {status}，实际 {response_status}")
            if status == 200:
                validate_success(response, data)
            else:
                if not all(key in response for key in ("request_id", "code", "message")):
                    raise AssertionError("错误形状缺字段")
            for key, value in (fields or {}).items():
                if response.get(key) != value:
                    raise AssertionError(f"{key} 与预期不一致")
            return {"http_status": response_status, "response": response}

        try:
            service.start()
            record("请求编号、修订、算法版本与三句参数", lambda: request(200, {"duration_ms": 12000, "phrase_count": 3}))
            record("非法令牌", lambda: request(401, {"code": "INVALID_TOKEN", "request_id": None}, credential=token + "-wrong"))
            record("缺失令牌", lambda: request(401, {"code": "INVALID_TOKEN"}, credential=""))
            record("坏音频", lambda: request(415, {"code": "UNSUPPORTED_AUDIO", "request_id": base_metadata["request_id"]}, payload=b"not-a-wav"))
            record("截断 WAV", lambda: request(415, {"code": "UNSUPPORTED_AUDIO"}, payload=audio[:-5]))
            record("超 10 分钟", lambda: request(413, {"code": "TOO_LARGE", "field": "audio"}, payload=overlong_audio()))
            record("metadata 超限", lambda: request(413, {"code": "TOO_LARGE", "field": "metadata"}, raw=b" " * (MAX_METADATA_BYTES + 1)))
            duplicate = copy.deepcopy(base_metadata)
            duplicate["phrases"][1]["id"] = duplicate["phrases"][0]["id"]
            record("重复句 ID", lambda: request(422, {"code": "INVALID_BOUNDARY", "field": "phrases[1].id"}, metadata=duplicate))
            invalid = copy.deepcopy(base_metadata)
            invalid["phrases"][2]["end_ms"] = 12001
            record("第三句越界", lambda: request(422, {"field": "phrases[2].end_ms"}, metadata=invalid))
            record("非法 JSON", lambda: request(400, {"code": "INVALID_INPUT", "field": "metadata"}, raw=b"{invalid"))

            def oversized_header():
                connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=3)
                try:
                    connection.putrequest("POST", INSPECT_PATH)
                    connection.putheader("Authorization", "Bearer " + token)
                    connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
                    connection.endheaders()
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    if response.status != 413 or body.get("code") != "TOO_LARGE":
                        raise AssertionError("超限请求头没有提前拒绝")
                    return {"http_status": response.status, "response": body,
                            "note": "仅发送超限长度头，未上传 100 MiB 文件"}
                finally:
                    connection.close()
            record("超限请求在上传前拒绝", oversized_header)

            baseline = request(200)["response"]
            def repeat():
                result = request(200)
                if result["response"] != baseline:
                    raise AssertionError("相同请求结果变化")
                return result
            record("相同请求重放一致", repeat)
            revised = copy.deepcopy(base_metadata)
            revised["revision_id"] = "d3-revision-2"
            record("相同请求编号的新修订不会返回旧修订", lambda: request(200, metadata=revised))

            def mismatched_response():
                wrong = dict(baseline, revision_id="wrong-revision")
                try:
                    validate_success(wrong, base_metadata)
                except ValueError:
                    return {"note": "调用侧校验函数拒绝错修订；不是 A 业务进程测试"}
                raise AssertionError("错修订未被拒绝")
            record("调用侧拒绝错修订结果", mismatched_response)
            service.stop()

            def unavailable():
                try:
                    send_inspect(service.url, token, audio, json.dumps(base_metadata).encode(), timeout=1)
                except OSError:
                    return {"note": "C 停止后连接失败，调用方应保留可重试状态"}
                raise AssertionError("C 已停止，但调用未失败")
            record("C 停止时调用不可用", unavailable)
            service.start()
            record("原端口重启后同包重试结果一致", repeat)
        finally:
            service.stop()
        after = sorted(p.relative_to(temporary).as_posix() for p in Path(temporary).rglob("*"))
        def no_temporary_audio():
            if before != after:
                raise AssertionError("服务配置的临时目录出现新增文件")
            return {"files_before": before, "files_after": after,
                    "note": "仅验证指定 TMP/TEMP/TMPDIR；不等价全系统磁盘审计"}
        record("成功、失败及重启后临时目录无残留", no_temporary_audio)

    return {
        "date": datetime.now(timezone.utc).astimezone().isoformat(),
        "stage": "D3 C 独立服务", "processor_version": PROCESSOR_VERSION,
        "scope": "真实 C 子进程异常/恢复测试；没有调用 A 业务后端或验证课程发布",
        "case_count": len(cases), "passed_count": sum(row["passed"] for row in cases),
        "all_passed": all(row["passed"] for row in cases), "cases": cases,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="D3 异常输入、版本与停止/重启验证")
    parser.add_argument("--output", type=Path, default=ROOT / "ai_backend/reports/d3_http_results.json")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    # This runner only writes new JSON reports or replaces its own report schema.
    if output.suffix.lower() != ".json":
        parser.error("报告输出必须使用 .json 扩展名")
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            parser.error(f"不能覆盖非 D3 报告文件：{error}")
        if not isinstance(existing, dict) or existing.get("stage") != "D3 C 独立服务":
            parser.error("只能覆盖既有 D3 报告，不能覆盖输入或其他文件")
    report = run_d3()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in report["cases"]:
        print(("通过" if row["passed"] else "失败") + "  " + row["case"])
    print(f"D3：{report['passed_count']}/{report['case_count']}；报告：{output}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
