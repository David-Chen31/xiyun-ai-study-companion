# D1 技术样例说明（C 构造，无授权素材）

全部由 `python -m ai_backend.make_samples` 程序合成，不含真实人声，可自由分发重放。

| 文件 | 内容 | 预期结果 |
|---|---|---|
| `demo_12s.wav` | 12 秒整、16000 Hz、单声道、16-bit PCM；三个句窗（0.5–3.9 s、5–8 s、9–12 s）内为轻音（约 -18 dBFS，5 ms 淡入淡出），其余静音 | `duration_ms=12000` |
| `success.json` | 契约单句测例，request_id=q7，句 p2 | 200，`phrase_count=1` |
| `success_three.json` | 契约三句课包，request_id=q9，第三句 `entry_ms=null`（仅听练） | 200，`phrase_count=3` |
| `invalid_boundary.json` | request_id=q8，`end_ms=13000` 超出 12 秒音频 | 422 `INVALID_BOUNDARY`，`field=phrases[0].end_ms` |

句窗与契约课程样例 c1/r1 的三句时间一致，便于 D2 与真实课包对照。