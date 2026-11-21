# C · AI 音频检查服务

戏韵 AI 学伴现场版，C 交付给 A 的独立音频检查服务。沿用 [API 草案](../docs/team/API_CONTRACT.md) 中的 `POST /internal/v1/inspect`：接收 WAV 与句表 metadata，实测音频参数、检查每句时间界限，返回结构化结果或精确错误字段。

最新安排（2026-11-21）：A 让 C 先自行找基础素材，参数与返回格式由 C 暂定，后续再统一接口。当前已提供公开语音、短音效和合成三句的完整技术验证，不再等待教师课包才推进这个阶段。[临时接口说明](临时接口说明.md) 是本阶段调用依据；公共契约仍由 A 统一维护。

## 先运行这一条

在包含 `ai_backend` 的项目根目录执行，无需先开服务或配置令牌：

```powershell
python -X utf8 -m ai_backend.check_basics
```

程序自动启停本机临时服务，校验素材文件，发送真实 HTTP 请求。预期 **12/12 通过**；[基础验证报告](基础素材验证报告.md) 记录了结果，[基础素材说明](samples/basic/README.md) 记录来源与许可。服务仍只做 WAV 参数与时间范围检查，未实现语音识别、自动分句或评分。

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

59 项测试，含单元级（WAV 实测、句界、multipart 二进制往返、本机报告）与真实 HTTP 级（回环随机端口起服务，覆盖 200/400/401/404/413/415/422）。使用程序合成样本及本仓库随附的公开来源素材，不依赖网络下载或教师音频。D1 的 37 项、D2 首轮 56 项历史记录保留在对应报告中，最新结果见 [基础素材验证报告](基础素材验证报告.md)。

## D2：三句验证与真实文件检查

先使用仓库的合成三句样例验证软件。以下命令可直接用于 Windows PowerShell，不需要启动 HTTP 服务：

```powershell
python -X utf8 -m ai_backend.d2_validate --audio ai_backend/samples/demo_12s.wav --metadata ai_backend/samples/success_three.json --source "程序合成测试音"
python -X utf8 -m ai_backend.d2_validate --audio ai_backend/samples/demo_12s.wav --metadata ai_backend/samples/invalid_three_boundary.json --source "程序合成测试音"
```

第一条返回 `status=passed`、`duration_ms=12000`、`phrase_count=3`，第三句入点为空也通过。第二条返回 `http_status=422`、`field=phrases[2].end_ms`，退出码 1 是预期的失败样例结果。

`d2_validate` 是本机检查工具，输出的 `http_status` 表示同一校验结果在服务接口对应的状态码；A 的真实 HTTP 联调需使用上一节的 `replay` 或业务后端客户端。

换用任意符合格式的基础音频和技术句表时，将上面命令的两个输入路径替换为实际文件。可选 `--source "样本来源说明" --output "本机报告路径.json"` 保存结果。报告包括音频 SHA-256、实际帧数、时长、采样率、声道、位深、耗时，以及成功结果或错误字段。报告只写在本机，工具不上传或复制音频。

本轮基础素材技术验证已完成。后续教师课包的专业复核和 A 的业务后端接入另行验收；当前技术窗口不冒充专业句界。重复句 ID 的状态码、100 MB 的具体字节口径等由 C 在临时说明中明确，等待接口统一。WAV 额外检查容器长度、块完整性、帧宽与字节率；只支持单个 data 块，标准附加元数据块允许存在。WAV 扩展 PCM 格式依赖 Python 的 wave 支持；当前已验证的运行环境是 Python 3.13.5。

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
