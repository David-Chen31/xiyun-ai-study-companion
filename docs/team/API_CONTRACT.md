# 五天现场版 API v1｜开工联调用

B → A 是浏览器同源请求；A → C 是仅回环访问的内部 HTTP。A 的服务提供静态网页、数据库和私有媒体；A 启动两项服务并向 C 配置内部 Bearer 令牌。令牌只在服务端。C 无业务库账号，不访问浏览器。以下数据为测试约定，D1 三人确认后冻结。课程流程、未来平台 API 见[架构方案](../02-设计与架构深化/架构与接口.md)。

## 作业怎么进入 A

本机操作员运行拟定命令 `python -m backend import-course --manifest lesson.json --audio demo.wav`。清单含下例的 course_id、revision_id、media_id、title、credit、phrases，以及 author_id、reviewer_id、grant_ref（三者均为字符串）、expires_at（UTC 时间）。许可台账另记同一音频 SHA-256、允许现场播放及技术处理的用途、署名与期限。A 拒绝自审、用途缺失及过期。课程 ID 绑定不可变快照，相同内容重复导入返回原状态，同 ID 不同内容报冲突；重发用新 ID。撤回命令 `python -m backend withdraw --course c1` 可重复使用。音频保存在公开目录之外。

## B 向 A 读作业和取流

GET /v1/courses/c1 无请求体。200 响应示例（所有 ID 与文本为字符串，时间为原 WAV 零点起的整数毫秒）：

```json
{"course_id":"c1","revision_id":"r1","media_id":"m1","title":"三句作业","credit":"示范老师","duration_ms":12000,"stream_url":"/v1/media/m1/stream?course_id=c1","phrases":[{"id":"p1","text":"甲","lead_ms":0,"start_ms":500,"entry_ms":600,"end_ms":3900},{"id":"p2","text":"乙","lead_ms":4000,"start_ms":5000,"entry_ms":5100,"end_ms":8000},{"id":"p3","text":"丙","lead_ms":8100,"start_ms":9000,"entry_ms":null,"end_ms":12000}]}
```

phrases 数组顺序即课序，id 仅在所属快照内定位一句。entry_ms 可空，空时仅听练。B 保存 course_id、revision_id 与待练句 id。录音无上传接口。

GET stream_url 支持完整 200 和单一 Range（如 bytes=0-1023）206，返回 audio/wav、Accept-Ranges、Content-Length，206 附 Content-Range。每个请求重查许可，课程与音频均 no-store。非法范围 416 附总长度，未知作业 404 COURSE_UNKNOWN，不匹配 403 MEDIA_FORBIDDEN，已撤回或过期 410 COURSE_GONE。B 重新播放或回到页面时重验课程，收到 410 停播、清缓冲并解释原因；临时错误可重试。

## A 向 C 检查示范

POST /internal/v1/inspect，同步 multipart/form-data，Authorization: Bearer <内部令牌>。字段 audio 是原始 WAV 文件；metadata 是 UTF-8 JSON 文本，必含 request_id、revision_id 和与上例相同结构的 phrases。例如单句测例：

```json
{"request_id":"q7","revision_id":"r1","phrases":[{"id":"p2","text":"乙","lead_ms":4000,"start_ms":5000,"entry_ms":5100,"end_ms":8000}]}
```

C 限制文件 ≤100 MB、≤10 分钟、16-bit PCM WAV、单/双声道。用实际帧数检查时长；每句须满足 0 ≤ lead_ms ≤ start_ms < end_ms ≤ duration_ms，非空 entry_ms 位于 [lead_ms,end_ms)。拒绝重复句 ID、空数组和截断文件。200 成功示例（时长、帧率、声道、句数均为整数）：

```json
{"request_id":"q7","revision_id":"r1","processor_version":"wav-inspect-1","duration_ms":12000,"sample_rate_hz":16000,"channels":1,"phrase_count":1}
```

错误统一形状。示例为 422：

```json
{"request_id":"q7","code":"INVALID_BOUNDARY","message":"结束时间超出音频","field":"phrases[0].end_ms"}
```

field 可省略。400 INVALID_INPUT、401 INVALID_TOKEN、413 TOO_LARGE、415 UNSUPPORTED_AUDIO、422 INVALID_BOUNDARY、503 ANALYSIS_UNAVAILABLE。C 只读输入，处理完释放临时文件，无发布副作用。A 核对回传请求/修订与当前草稿，30 秒超时按服务不可用处理，保留可重试的导入记录；遇输入错误请操作员修改，服务故障恢复后用同一包重试。A 仅在结果相符、许可仍有效时发布，失败不替换旧快照。已发布作业播放无需调用 C。

D1 B 以课程样例和 410 测网页；A 用 200、422、超时模拟 C；C 用自生成的 12 秒 WAV 单测并接受 A 的真实请求。更改单位、字段、路径、状态码或隐私边界，提供旧新样例与迁移步骤，通知所有消费者，经共同评审再修改。
