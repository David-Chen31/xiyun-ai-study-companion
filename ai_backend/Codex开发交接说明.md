# Codex 开发交接说明：C 音频检查服务

更新日期：2026-11-22。本文件供 A 及其在 Qoder CN 中使用的开发助手阅读。

## 1. 当前交付与最新决定

项目成员 C 交付独立运行的音频检查服务。A 最新安排：C 先自己寻找基础素材，定义临时接收参数和返回格式，跑通能力；后续再与 A 的业务后端统一接口。用户已明确允许提交 Codex 开发的内容，不要求全程在 Qoder CN 制作。A 可以在 Qoder 中读取本说明、检查代码并继续开发。

开发来源保留：D1 基础由成员 C 在 Qoder CN 开发，D2 补强、基础素材包、测试工具和本文档由 Codex 完成。后续 Qoder 的阅读、验证或修改可单独记录，不改写既有来源。

交付分支：`feat/ai-qoder-d1`。虽然分支名保留 qoder-d1，但它已经包含后续 Codex 改动。上传目标是成员的 Fork，向 `David-Chen31/xiyun-ai-study-companion` 发起 PR #2；不直接合并 main。

核心结果：WAV 实测参数和时间范围检查可独立运行；最新 95 项回归测试通过，基础素材 12 个 HTTP 用例和 D3 独立服务 17 个场景通过。A 的业务导入、发布、撤回、失败重试和正式接口适配仍待接入。

## 2. 推荐阅读顺序

先读项目根目录的 `AGENTS.md`、`docs/README.md` 和 `docs/产品设计与研发协作流程.md`，了解项目阶段；再读 `docs/team/C-AI后端.md`、`docs/team/TEAM_PLAN.md` 及 `docs/team/API_CONTRACT.md`。公共契约中的现场版草案与本轮 A 允许 C 暂定接口的安排需要一起理解，不能把当前临时格式当作团队已经冻结的最终契约。

接着阅读本文件，以及以下 C 目录材料：

| 材料 | 说明 |
|---|---|
| [README.md](README.md) | 启动方式和快速运行入口 |
| [临时接口说明.md](临时接口说明.md) | 当前完整输入、输出、错误码和调用示例 |
| [基础素材验证报告.md](基础素材验证报告.md) | 最新基础素材实测与当前完成范围 |
| [D3验证报告.md](D3验证报告.md) | 最新异常、结果核对、停机重试与 A 的业务联调清单 |
| [D2验证报告.md](D2验证报告.md) | WAV 完整性修复、三句检查与首轮结果 |
| [开发与测试记录.md](开发与测试记录.md) | D1 历史开发记录，37 项测试是当时结果 |
| [samples/basic/manifest.json](samples/basic/manifest.json) | 基础素材来源、Git blob、哈希、转换与预期参数 |

历史报告中的“还只有合成样本”“37/56/59 项”仅描述当时状态；最新状态以 D3 报告及代码实际运行结果为准。

## 3. 环境与复现

当前验证环境：Windows、CPython 3.13.5。服务运行、测试、重放均只使用标准库；`requirements.lock` 记录无第三方运行依赖。README 声明 Python 3.10 及以上，但并未逐版本实测，尤其 WAV 扩展 PCM 的支持需要在目标 Python 版本复测。

所有命令均在仓库根目录执行，即当前目录能看到 `ai_backend` 文件夹。

```powershell
python -X utf8 -B -m unittest discover -s ai_backend/tests -t . -v
```

最近已完成的回归为 95 项、结果 OK。D3 修改了可执行逻辑并已完整复测，历史 59 项是此前阶段结果。

```powershell
python -X utf8 -m ai_backend.check_basics
```

这条命令无需手动启动服务或设置令牌：校验随包素材 SHA-256，自动启动空闲回环端口的临时 C 服务，经 HTTP 发送 12 个用例，写报告并关闭该服务。预期 12/12，退出码 0。报告默认保存在 `ai_backend/reports/basic_http_results.json`；再次执行会更新耗时，可用 `--output` 指定另一个本机报告路径。

手动启动服务：

```powershell
$env:AI_BACKEND_TOKEN = "c-local-demo-token"
python -X utf8 -m ai_backend --host 127.0.0.1 --port 8300
```

另一个窗口进入相同项目目录，执行：

```powershell
python -X utf8 -m ai_backend.replay --token c-local-demo-token --audio ai_backend/samples/basic/jfk-speech.wav --metadata ai_backend/samples/basic/speech.json
```

预期 200，11000 ms、44100 Hz、双声道、3 时间窗。示例令牌仅用于本机演示，正式值由 A/C 配置，不进浏览器。

如需在已启动的服务上完整重放：

```powershell
$env:AI_BACKEND_TOKEN = "c-local-demo-token"
python -X utf8 -m ai_backend.check_basics --url http://127.0.0.1:8300 --output ai_backend/reports/local_recheck.json
```

实际验收包含过独立服务进程和独立调用进程，结果见 `reports/basic_http_external_results.json`；它验证 C 的调用通路，不表示 A 业务进程已经接入。

D3 可运行 `python -X utf8 -m ai_backend.check_d3`，自动启停与重启专用服务子进程，预期 17/17。它验证 C 停止时连接失败、同端口重启后同包重试一致、指定临时目录无残留。报告默认保存在 `reports/d3_http_results.json`；A 的“课程只发布一次”仍需业务后端验证。

## 4. 代码职责和处理流程

| 文件 | 主要职责 |
|---|---|
| `__main__.py` | 命令行/环境配置，服务生命周期；参数优先于环境变量 |
| `server.py` | POST 路由、Bearer 鉴权、请求限额、JSON 解析和错误响应 |
| `multipart.py` | 标准库 MIME 解析与附加约束，拒绝重复字段/截断并保留字节 |
| `inspection.py` | metadata 类型、RIFF 容器、WAV 帧数和每句时间范围检查 |
| `replay.py` | `send_inspect`：发送 multipart 请求，返回状态码与 JSON |
| `d2_validate.py` | 不启动 HTTP 的本机文件检查，可保存帧数/位深/哈希/耗时 |
| `check_basics.py` | 基础素材校验、临时服务启停、12 个 HTTP 用例与报告 |
| `check_d3.py` | D3 17 个独立子进程场景，包括停机、同端口重启和重试 |
| `make_samples.py` | 生成 12 秒合成 WAV、三句及越界 metadata |
| `tests/` | 元数据、边界、WAV、HTTP、报告与素材回归测试 |

HTTP 请求进入 `server.py` 后，先检查路径和内部令牌，再检查唯一 Content-Length、请求体大小及 multipart。读取体的总期限默认 10 秒；认证错误先响应，再短暂丢弃上传数据。解析出 audio 和 metadata 后，尽可能保留 request_id，调用 `inspection.inspect`。响应带 no-store 并关闭连接。

`inspect` 按顺序校验 metadata、实测 WAV、校验每句范围，最后返回结构化结果。业务错误抛 `InspectError`，由服务转换为相应状态和错误 JSON；意外内部异常按 503 处理。

WAV 检查先遍历 RIFF 块，核对文件声明长度、块边界、fmt/data 唯一性、16-bit、声道、帧宽和字节率，再通过 `wave` 读取实际帧。文件字节必须能组成完整采样帧，实际读取字节数须与帧数匹配。时长按 `frames * 1000 // sample_rate` 得到整数毫秒；600 秒上限在毫秒取整之前按精确帧数判断。

服务不写业务库、不发布课程、不持有业务库账号。输入音频在内存中处理，没有临时音频落盘；因此重复请求重新计算但不会重复发布。

## 5. 临时接口摘要

默认 `POST /internal/v1/inspect`，地址 `127.0.0.1:8300`，Bearer 令牌。multipart 中 audio 为原始 WAV，metadata 为 UTF-8 JSON 文本。完整规范和例子以 [临时接口说明](临时接口说明.md) 为准。

metadata 必须有非空 request_id、revision_id 和非空 phrases 数组。每句含字符串 id/text、整数 lead_ms/start_ms/end_ms，以及显式的整数或 null entry_ms。时间单位为原始 WAV 零点起的整数毫秒，排除 bool。

边界：`0 ≤ lead_ms ≤ start_ms < end_ms ≤ duration_ms`；非空入点位于 `[lead_ms, end_ms)`。重复句 ID 拒绝；不额外要求各句连续或不重叠；不做语义句界判断。

成功结果包含 request_id、revision_id、processor_version、duration_ms、sample_rate_hz、channels、phrase_count。处理器版本仍为 `wav-inspect-1`。

| 状态 | code | 主要用途 |
|---|---|---|
| 400 | INVALID_INPUT | JSON、字段或类型不合法 |
| 401 | INVALID_TOKEN | 认证失败 |
| 413 | TOO_LARGE | 文件、请求或时长超限 |
| 415 | UNSUPPORTED_AUDIO | 不支持格式、容器损坏或帧截断 |
| 422 | INVALID_BOUNDARY | 时间边界错误或重复句 ID |
| 503 | ANALYSIS_UNAVAILABLE | 内部意外错误 |

错误包含 request_id/code/message，可定位时带 field，例如 `phrases[2].end_ms`。无法解析 request_id 或认证前失败时为 null。参数检查工具报告里的额外字段不会出现在 HTTP 成功响应里。

## 6. 重要实现决定

- 大小上限当前明确为 104857600 字节（100 MiB）；公共草案写 100 MB，最终十进制/二进制口径须与 A 统一。HTTP 总请求额外预留 1 MiB 表单开销。
- D3 metadata 限 256 KiB，JSON 最多 64 层，拒绝重复键、非有限数值、非法 Unicode 和空白 ID。调用侧可以使用 `replay.validate_success` 核对返回编号、修订、版本与参数。
- 目前限定 16-bit PCM WAV、单/双声道、600 秒；不自动转码音频。
- 单个 fmt/data 块，完整的 JUNK 等附加元数据块可接受；多 data、容器长度不一致拒绝。
- 缺少 entry_ms 是 400；entry_ms=null 是合法听练输入。此修复避免缺字段时落入 503。
- 默认监听回环。命令行仍允许指定其他地址并打印警告，尚未强制禁止。
- 日志不输出令牌和音频；D3 只输出精简访问记录，转义控制字符。临时配置、日志和报告不等于业务审计系统。
- 旧实现 `feat/ai-inspect` 保留在独立分支，当前交付是 `feat/ai-qoder-d1`，不要误用旧版 8001 端口及 `examples` 命令。

## 7. 素材与测试证据

公开语音来源为 Whisper 仓库测试 FLAC，制备阶段转成 PCM16 WAV，保留 44100 Hz、双声道和 485100 帧。短音效与 8-bit 反例来自 CPython 测试文件。许可证副本、Git blob 与哈希随素材保留。转换工具只在工作目录用于制备，服务无需安装其依赖。

语音分成 0–3600、3600–7300、7300–11000 ms 三个技术窗，第三入点 null；这些窗口不代表教师复核、自动分句或真实起唱金标。不要据此评价戏曲教学效果或算法识别准确率。

素材实测、用例清单和结果见 [基础素材验证报告](基础素材验证报告.md) 与 [D3验证报告](D3验证报告.md)。95 项是当前回归总数；12 个基础素材用例与 17 个 D3 场景均被回归工具部分覆盖，不能将数字简单相加为独立测试总数。

100 MiB 文件限额使用缩小阈值的逻辑测试，没有实际大文件压力测试。已有 44.1/48 kHz 双声道、精确 600 秒加一帧、容器截断、半采样帧、空入点、字段定位等覆盖。旧 D1 修复过一条测试夹具自身时间关系错误，历史记录保留；不代表当前还有该失败。

## 8. 尚未完成与接手时优先检查

本轮没有实现 ASR、自动句界、起唱检测、音高节奏评分、知识问答，也没有完成 A 的业务导入发布流程。后续研究能力需按功能分别定义输入输出与评测条件。

接入前建议由 A/Qoder 核对以下已知限制，按当前实际需求逐项处理；本交接不把这些工作标成已完成：

1. HTTP 服务无并发压测，音频和 multipart 全量在内存处理，需要评估大文件并发及资源限额。
2. D3 已增加 10 秒请求体总读取期限并验证超时后恢复；认证先回复再有界丢弃。网络过慢或提前拒绝大上传时仍可能出现传输异常，A 需要处理。
3. D3 已用标准库 MIME 解析并测试重复字段、缺结束边界、boundary 字节等情形。仍不接受 chunked、嵌套或编码转换，A 应按临时说明构造上传。
4. 请求编号只回传不落库去重；C 无发布副作用，A 必须完成导入去重、事务、旧修订防覆盖和重试语义。
5. D3 已测试非 ASCII 客户端令牌返回 401、非法配置拒绝启动、命令行端口覆盖环境变量。配置令牌明确限定为可打印无空白 ASCII。
6. 健康检查接口未定义；跨机访问、身份/许可和浏览器对 C 直连均未设计。本轮默认 A/C 同机，浏览器仍只访问 A。
7. 第二个干净环境、真实教师复核课包、A 停止 C 后旧课可练与恢复重试的业务验证尚未完成。

## 9. A 与 Qoder 的建议接手步骤

1. 获取 PR #2 的最新代码，确认处于当前交付分支而非只打开 main；先检查本机未提交改动，保留他人工作。
2. 按前述阅读顺序查阅资料，然后执行 95 项测试、12 用例基础重放和 17 场景 D3 验证，记录本机实际结果，不只引用本报告。
3. 将 `server.py` 视为传输适配层，将 `inspection.py` 的无业务依赖计算作为复用边界；不要为了对接重写已经通过的全部逻辑。
4. 与 A 统一时间单位、字段、错误码、格式限额、令牌配置、同步超时和部署方式。需要改变契约时保留新旧请求例子，更新对应测试和文档。
5. A 的业务进程至少发一条成功与一条越界请求，核对 request_id/revision_id，再测不可用与重试；C 的本机报告不可替代 A 的真实结果。
6. 记录 Qoder 实际读取、验证、修改的内容，以及仍待完成的项目；开发来源延续记录，不需要为工具名称重新制作已获准提交的代码。

## 10. 可直接交给 Qoder 的任务描述

```text
请从 ai_backend/Codex开发交接说明.md 开始阅读，再按其中列出的项目规则、接口说明、代码入口和报告核对当前 C 服务。

项目允许提交 Codex 开发的内容，不要求全程在 Qoder CN 重新制作。保留原始开发来源与现有改动，先确认实际分支和工作区状态。

先运行 95 项回归测试、ai_backend.check_basics 的 12 个真实 HTTP 用例及 ai_backend.check_d3 的 17 个场景，记录实际结果。读取临时接口说明，并和 A 当前业务后端的实现比较，列出需要统一的路径、字段、单位、限额、错误码、令牌和超时。

范围内的对接按项目目录所有权推进。检查本交接列出的未完成项目，不把本机技术验证当作 A 业务验收，不把技术时间窗当作教师金标，也不宣称已经完成 ASR 或评分。

完成后给出实际改动、成功和失败调用结果、验证方式与剩余事项。
```
