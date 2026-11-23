"""生成 D1 技术样例：python -m ai_backend.make_samples

产出 ai_backend/samples/ 下：
- demo_12s.wav        12 秒、16 kHz、单声道、16-bit PCM，句窗内为轻音，其余静音；
- success.json        契约单句成功测例（request_id=q7）；
- success_three.json  契约三句课包测例（request_id=q9，第三句 entry_ms=null）；
- invalid_boundary.json  end_ms=13000 超出音频的 422 反例（request_id=q8）。

样例为程序合成的技术样本，不含任何真实人声与授权素材。
"""

from __future__ import annotations

import array
import copy
import json
import math
import wave
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
SAMPLE_RATE = 16000
DURATION_S = 12
# 与契约三句课包一致的句窗（毫秒），窗内发轻音便于试听核对。
WINDOWS = [(500, 3900), (5000, 8000), (9000, 12000)]


def make_wav(path: Path) -> None:
    samples = array.array("h")
    for n in range(SAMPLE_RATE * DURATION_S):
        t_ms = n * 1000 / SAMPLE_RATE
        value = 0
        for k, (start, end) in enumerate(WINDOWS):
            if start <= t_ms < end:
                freq = 330.0 + 110.0 * k
                # 5 ms 淡入淡出，避免爆音；振幅约 -18 dBFS。
                fade = min(1.0, (t_ms - start) / 5.0, (end - t_ms) / 5.0)
                value = int(4000 * fade * math.sin(2 * math.pi * freq * n / SAMPLE_RATE))
                break
        samples.append(value)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    make_wav(SAMPLES_DIR / "demo_12s.wav")
    cases = {
        "success.json": {
            "request_id": "q7", "revision_id": "r1",
            "phrases": [
                {"id": "p2", "text": "乙", "lead_ms": 4000, "start_ms": 5000, "entry_ms": 5100, "end_ms": 8000},
            ],
        },
        "success_three.json": {
            "request_id": "q9", "revision_id": "r1",
            "phrases": [
                {"id": "p1", "text": "甲", "lead_ms": 0, "start_ms": 500, "entry_ms": 600, "end_ms": 3900},
                {"id": "p2", "text": "乙", "lead_ms": 4000, "start_ms": 5000, "entry_ms": 5100, "end_ms": 8000},
                {"id": "p3", "text": "丙", "lead_ms": 8100, "start_ms": 9000, "entry_ms": None, "end_ms": 12000},
            ],
        },
        "invalid_boundary.json": {
            "request_id": "q8", "revision_id": "r1",
            "phrases": [
                {"id": "p2", "text": "乙", "lead_ms": 4000, "start_ms": 5000, "entry_ms": 5100, "end_ms": 13000},
            ],
        },
    }
    three_invalid = copy.deepcopy(cases["success_three.json"])
    three_invalid["request_id"] = "q10"
    three_invalid["phrases"][2]["end_ms"] = 12001
    cases["invalid_three_boundary.json"] = three_invalid
    for name, payload in cases.items():
        (SAMPLES_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"样例已生成于 {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
