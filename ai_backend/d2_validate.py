"""D2 验证工具：对一个真实 WAV 文件和句表执行完整 inspect。

用法（仓库根目录）：
  python -m ai_backend.d2_validate --audio path/to/demo.wav --metadata path/to/phrases.json

工具只读输入，不上传、不发布，也不写入音频临时文件。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .inspection import InspectError, inspect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 WAV 实际参数与三句时间范围")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect(args.audio.read_bytes(), json.loads(args.metadata.read_text(encoding="utf-8")))
    except InspectError as error:
        print(json.dumps({"status": "failed", **error.to_body(None)}, ensure_ascii=False, indent=2))
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(json.dumps({
            "status": "failed", "code": "INVALID_INPUT", "message": str(error),
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "passed", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
