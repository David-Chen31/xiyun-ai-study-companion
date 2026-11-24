"""One-command basic fixture verification over real loopback HTTP.

python -m ai_backend.check_basics
python -m ai_backend.check_basics --url http://127.0.0.1:8300

Without --url, start and stop a temporary C service on a free loopback port.
With --url, read AI_BACKEND_TOKEN and test an already-running C service.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time

from . import PROCESSOR_VERSION
from .replay import send_inspect
from .server import make_server

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "ai_backend/samples/basic/manifest.json"


def load_assets():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assets = {}
    for item in manifest["assets"]:
        audio = (ROOT / item["audio"]).read_bytes()
        digest = hashlib.sha256(audio).hexdigest()
        if digest != item["sha256"]:
            raise ValueError("样本校验值不匹配：" + item["id"])
        metadata = json.loads((ROOT / item["metadata"]).read_text(encoding="utf-8"))
        assets[item["id"]] = {"info": item, "audio": audio, "metadata": metadata}
    return manifest, assets


def run_cases(base_url, token):
    manifest, assets = load_assets()
    rows = []

    def run(name, asset_id, *, edit=None, raw_metadata=None, audio=None, supplied_token=None,
            status=200, fields=None, request_id=None):
        asset = assets[asset_id]
        metadata = copy.deepcopy(asset["metadata"])
        if edit:
            edit(metadata)
        request_bytes = (json.dumps(metadata, ensure_ascii=False).encode("utf-8")
                         if raw_metadata is None else raw_metadata)
        expected = {"request_id": metadata["request_id"] if request_id is None else request_id}
        if status == 200:
            expected.update(asset["info"]["expected"])
            expected.update(revision_id=metadata["revision_id"], processor_version=PROCESSOR_VERSION)
        if fields:
            expected.update(fields)
        started = time.perf_counter()
        try:
            actual_status, response = send_inspect(
                base_url, token if supplied_token is None else supplied_token,
                asset["audio"] if audio is None else audio, request_bytes,
            )
            mismatches = []
            if actual_status != status:
                mismatches.append(f"HTTP 预期 {status}，实际 {actual_status}")
            for key, value in expected.items():
                if response.get(key) != value:
                    mismatches.append(f"{key} 预期 {value!r}，实际 {response.get(key)!r}")
            row = {"case": name, "asset": asset_id, "passed": not mismatches,
                   "expected_http_status": status, "http_status": actual_status,
                   "response": response, "mismatches": mismatches}
        except Exception as error:
            row = {"case": name, "asset": asset_id, "passed": False,
                   "expected_http_status": status, "error": str(error)}
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
        return row

    run("公开语音三窗口与空入点", "public-speech")
    run("公开短音效参数", "public-pluck")
    run("合成三句参数", "synthetic-three")
    run("第三句结束时间越界", "public-speech", status=422,
        edit=lambda data: data["phrases"][2].update(end_ms=11001),
        fields={"code": "INVALID_BOUNDARY", "field": "phrases[2].end_ms"})
    run("入点等于结束时间", "public-speech", status=422,
        edit=lambda data: data["phrases"][0].update(entry_ms=3600),
        fields={"code": "INVALID_BOUNDARY", "field": "phrases[0].entry_ms"})
    run("缺失入点字段", "public-speech", status=400,
        edit=lambda data: data["phrases"][0].pop("entry_ms"),
        fields={"code": "INVALID_INPUT", "field": "phrases[0].entry_ms"})
    run("8-bit 格式拒绝", "public-pluck-unsupported-8bit", status=415,
        fields={"code": "UNSUPPORTED_AUDIO", "field": "audio"})
    run("截断语音拒绝", "public-speech", audio=assets["public-speech"]["audio"][:-7],
        status=415, fields={"code": "UNSUPPORTED_AUDIO", "field": "audio"})
    run("错误令牌", "public-speech", supplied_token=token + "-wrong", status=401,
        fields={"code": "INVALID_TOKEN", "request_id": None})
    run("非法 metadata JSON", "public-speech", raw_metadata=b"{invalid-json", status=400,
        fields={"code": "INVALID_INPUT", "field": "metadata", "request_id": None})
    first = run("重复请求第一次", "public-speech")
    second = run("重复请求第二次", "public-speech")
    if first.get("response") != second.get("response"):
        second["passed"] = False
        second.setdefault("mismatches", []).append("相同请求两次返回不一致")
    return {
        "pack_version": manifest["pack_version"], "processor_version": PROCESSOR_VERSION,
        "scope": "C 独立服务的基础素材验证；不代表 A 业务联调或教师专业复核",
        "assets": [{"id": a["info"]["id"], "source_kind": a["info"]["source_kind"],
                    "sha256": a["info"]["sha256"], "measured_offline": a["info"]["measured_offline"]}
                   for a in assets.values()],
        "case_count": len(rows), "passed_count": sum(r["passed"] for r in rows),
        "all_passed": all(r["passed"] for r in rows), "cases": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="一条命令完成基础素材 HTTP 验证")
    parser.add_argument("--url", help="可选：测试已启动的 C 服务；不填则自动启动临时服务")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "ai_backend/reports/basic_http_results.json")
    args = parser.parse_args(argv)
    if args.url and not os.environ.get("AI_BACKEND_TOKEN"):
        parser.error("测试已启动服务时，请先设置 AI_BACKEND_TOKEN")
    # Verify the pack before starting a server or replacing an output file.
    try:
        manifest, _ = load_assets()
        protected = [MANIFEST]
        for asset in manifest["assets"]:
            protected.extend(ROOT / asset[key] for key in ("audio", "metadata"))
            if "license_file" in asset:
                protected.append(ROOT / asset["license_file"])
        if args.output.resolve() in {p.resolve() for p in protected}:
            parser.error("报告输出路径不能覆盖素材、许可证或句表")
    except (OSError, ValueError) as error:
        parser.exit(1, f"基础素材读取失败：{error}\n")
    server = None
    thread = None
    try:
        if args.url:
            report = run_cases(args.url, os.environ["AI_BACKEND_TOKEN"])
            report["service_mode"] = "existing_service"
        else:
            token = secrets.token_urlsafe(24)
            server = make_server("127.0.0.1", 0, token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            report = run_cases(f"http://127.0.0.1:{server.server_port}", token)
            report["service_mode"] = "temporary_loopback_service"
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in report["cases"]:
        label = "通过" if row["passed"] else "失败"
        print(f"{label}  {row['case']}  HTTP {row.get('http_status', '连接失败')}")
    print(f"结果：{report['passed_count']}/{report['case_count']}；报告：{args.output}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
