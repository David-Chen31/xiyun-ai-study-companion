"""D2 验证工具：对一个真实 WAV 文件和句表执行完整 inspect。

用法（仓库根目录）：
  python -m ai_backend.d2_validate --audio path/to/demo.wav --metadata path/to/phrases.json

工具只读输入，不上传、不发布，也不写入音频临时文件。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import wave

from .inspection import MAX_FILE_BYTES, MAX_METADATA_BYTES, InspectError, decode_metadata, inspect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 WAV 实际参数与三句时间范围")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--source", default="未注明；请核对样本来源和复核记录", help="样本来源说明，写入本机报告")
    parser.add_argument("--output", type=Path, help="可选：保存本机 JSON 检查结果")
    args = parser.parse_args(argv)
    if args.output and args.output.resolve() in (args.audio.resolve(), args.metadata.resolve()):
        parser.error("输出路径不能覆盖音频或句表输入")
    started = time.perf_counter()
    request_id = None
    report = {"source": args.source}
    try:
        with args.metadata.open("rb") as metadata_source:
            metadata = decode_metadata(metadata_source.read(MAX_METADATA_BYTES + 1))
        if isinstance(metadata, dict) and isinstance(metadata.get("request_id"), str):
            request_id = metadata["request_id"]
        # 超限时只读取上限加一个字节，避免本机验证工具无界载入文件。
        with args.audio.open("rb") as source:
            audio = source.read(MAX_FILE_BYTES + 1)
        if len(audio) <= MAX_FILE_BYTES:
            report.update(audio_bytes=len(audio), audio_sha256=hashlib.sha256(audio).hexdigest())
        result = inspect(audio, metadata)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            report.update(frame_count=wav.getnframes(), sample_width_bits=wav.getsampwidth() * 8)
        report.update(status="passed", http_status=200, **result)
        exit_code = 0
    except InspectError as error:
        report.update(status="failed", http_status=error.status, **error.to_body(request_id))
        exit_code = 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        report.update(status="failed", http_status=400, request_id=request_id,
                      code="INVALID_INPUT", message=str(error))
        exit_code = 1
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:
            parser.exit(2, f"无法保存报告：{error}\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
