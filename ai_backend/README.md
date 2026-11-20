# C · AI 音频检查服务（D1）

戏韵 AI 学伴现场版，C 交付给 A 的独立音频检查服务。实现契约 [docs/team/API_CONTRACT.md](../docs/team/API_CONTRACT.md) 中的 `POST /internal/v1/inspect`：接收教师示范 WAV 与句表 metadata，实测音频参数、检查每句时间界限，返回结构化结果或精确错误字段。

**纯 Python 标准库实现，零第三方依赖。** 要求 Python ≥ 3.10（本次开发实测 3.13.5，Windows）。只读输入、内存中处理、不写临时文件、无业务库、无发布副作用。

## 一、启动（给 A）

在仓库根目录执行：

```bash
python -m ai_backend --host 127.0.0.1 --port 8300 --token <内部令牌>
```

也可以用环境变量配置（命令行参数优先）：

| 配置 | 环境变量 | 命令行 | 默认 |
|---|---|---|---|
| 监听地址 | `AI_BACKEND_HOST` | `--host` | `127.0.0.1`（契约要求仅回环） |
| 端口 | `AI_BACKEND_PORT` | `--port` | `8300` |
| 内部 Bearer 令牌 | `AI_BACKEND_TOKEN` | `--token` | 无，**必须提供**，否则拒绝启动 |

Windows PowerShell 示例：

```powershell
$env:AI_BACKEND_TOKEN = "由 A 生成的内部令牌"
python -m ai_backend --port 8300
```

启动成功会打印监听地址与处理器版本 `wav-inspect-1`；Ctrl+C 停止。令牌只在服务端配置与比较（常量时间比较），不出现在日志中。绑定非回环地址会打印警告。

## 二、可重放请求

样例在 [samples/](samples/README.md)，如缺失可重新生成：`python -m ai_backend.make_samples`。

方式一：自带重放客户端（标准库，A 可直接复用 `ai_backend/replay.py` 里的 `send_inspect` 函数）：

```bash
# 成功：应返回 200，duration_ms=12000
python -m ai_backend.replay --token <内部令牌> \
  --audio ai_backend/samples/demo_12s.wav \
  --metadata ai_backend/samples/success.json

# 失败：end_ms=13000 超出音频，应返回 422，field=phrases[0].end_ms
python -m ai_backend.replay --token <内部令牌> \
  --audio ai_backend/samples/demo_12s.wav \
  --metadata ai_backend/samples/invalid_boundary.json
```

方式二：curl（Git Bash / Linux / macOS）：

```bash
curl -s -X POST http://127.0.0.1:8300/internal/v1/inspect \
  -H "Authorization: Bearer <内部令牌>" \
  -F "audio=@ai_backend/samples/demo_12s.wav;type=audio/wav" \
  -F "metadata=<ai_backend/samples/success.json;type=application/json"
```

成功响应（实测输出）：

```json
{"request_id":"q7","revision_id":"r1","processor_version":"wav-inspect-1","duration_ms":12000,"sample_rate_hz":16000,"channels":1,"phrase_count":1}
```

越界响应（实测输出，HTTP 422）：

```json
{"request_id":"q8","code":"INVALID_BOUNDARY","message":"结束时间超出音频","field":"phrases[0].end_ms"}
```

## 三、检查规则与错误码

- 鉴权失败 → `401 INVALID_TOKEN`；请求体/metadata 结构或类型非法 → `400 INVALID_INPUT`（field 指明字段）。
- 文件 >100 MB 或时长 >10 分钟 → `413 TOO_LARGE`；非 16-bit PCM、非单/双声道、截断、零帧、非 WAV → `415 UNSUPPORTED_AUDIO`。
- 句界违反 `0 ≤ lead_ms ≤ start_ms < end_ms ≤ duration_ms`、非空 `entry_ms ∉ [lead_ms, end_ms)`、重复句 ID → `422 INVALID_BOUNDARY`，field 精确到 `phrases[i].字段`。
- 未预期内部故障 → `503 ANALYSIS_UNAVAILABLE`（A 可按契约重试）。
- `duration_ms` 由**实际读到的帧数**换算：`帧数 × 1000 ÷ 采样率`，向下取整为整数毫秒；头声明与实际数据不符按截断拒绝。
- 错误响应中的 `request_id` 在 metadata 可解析时始终回传，便于 A 对账。

## 四、独立测试

```bash
python -m unittest discover -s ai_backend/tests -t . -v
```

37 项测试，含单元级（WAV 实测、句界、multipart 二进制往返）与真实 HTTP 级（回环随机端口起服务，覆盖 200/400/401/404/413/415/422）。全部使用程序合成样本，不需要真实音频。

## 五、目录

| 文件 | 作用 |
|---|---|
| `__main__.py` | 服务入口与配置 |
| `server.py` | HTTP 路由、鉴权、限额、错误形状 |
| `inspection.py` | WAV 实测与句界检查（算法核心） |
| `multipart.py` | 二进制安全的 multipart 解析/构造 |
| `replay.py` | 可重放请求客户端 |
| `make_samples.py` | 技术样例生成器 |
| `tests/test_inspect.py` | 独立测试 |
| `samples/` | 12 秒 WAV 与成功/越界 metadata |
| `开发与测试记录.md` | 本次开发、实测结果与待 A 联调事项 |

Windows 控制台若出现中文乱码，可先执行 `chcp 65001` 或设置 `PYTHONUTF8=1`，不影响服务行为。