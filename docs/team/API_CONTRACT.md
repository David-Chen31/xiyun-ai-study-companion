# 五天现场演示契约 v0.1（D1 待三方确认）

沿用[候选平台契约](../02-设计与架构深化/架构与接口.md)中的课程、媒体、快照和毫秒语义。此处规定本地演示实际需要实现的子集。A 维护；B、C 分别确认 HTTP 消费和音频输入。D1 将下列样例固化为 contracts/ 下的共享测试数据，记录冻结提交。以下 ID 和凭证引用均为示例。

## 内容供给与发布

操作员在本地运行拟定命令 `python -m backend import-course --manifest lesson.json --audio demo.wav`。文件由老师线下提交，独立复核后导入；本轮关闭浏览器文件上传，页面与服务均无学员录音上传接口。撤回用 `python -m backend withdraw --course c1`。命令成功退出 0，写入/重复导入输出 course_id 和入口路径；无效输入退出 2、存储失败退出 1，stderr 输出下述错误对象。撤回重复执行成功。导入清单示例：

```json
{"course_id":"c1","revision_id":"r1","media_id":"m1","title":"示范作业","credit":"示范老师","author_id":"teacher1","reviewer_id":"reviewer2","grant_ref":"local-grant-1","expires_at":"2027-01-01T00:00:00Z","phrases":[{"id":"p1","text":"例词甲","lead_ms":0,"start_ms":1000,"entry_ms":1200,"end_ms":4000},{"id":"p2","text":"例词乙","lead_ms":4000,"start_ms":5000,"entry_ms":5100,"end_ms":8000},{"id":"p3","text":"例词丙","lead_ms":8000,"start_ms":9000,"entry_ms":null,"end_ms":12000}]}
```

ID、标题、署名、词与许可引用为非空字符串；expires_at 为 UTC 时间字符串；phrases 为按课序排列的非空数组，id 唯一。所有时间为原媒体非负整数毫秒，lead ≤ start < end ≤ duration；entry 可空，否则在 [lead,end) 内，仅表示教师提示。A 核对独立复核、预登记的本地许可台账、受众和期限，拒绝仅凭清单自证许可。C 检查 PCM WAV、16-bit、1–2 声道、时长 ≤10 分钟、文件 ≤100 MB、实际帧长与边界。演示包恰为三句。媒体由 A 复制至 Web 根外生成的私有键，计算 SHA-256；先验证后事务发布，失败清理临时文件。相同清单及内容重复导入返回原链接；相同课程 ID 不同内容拒绝 CONFLICT，新快照使用新课程 ID。撤回后重复导入不能恢复访问。凭证、个人原声及受限媒体均不进 Git。

## B → A：同源 HTTP /v1

演示限本机操作员和现场学员，服务只绑定 localhost，发给学员的入口为同一浏览器环境的 /course/c1。无远程身份或匿名班级令牌。若改为远程分发，先重新评审认证和许可。B 用本机键 course_id + revision_id 存待练句 ID，可主动清除。录音只用内存 Blob，停止录音时释放设备，离页或删除时回收 Blob URL。

|请求|成功|错误及界面行为|
|---|---|---|
|GET /v1/courses/{id}，无请求体|200，返回下例；禁止缓存|404 COURSE_UNKNOWN 显示作业不存在；410 COURSE_GONE 提示已撤回或过期并停播|
|GET /v1/media/{id}/stream?course_id=c1，可带 Range: bytes=0-1023|200 全量或 206 单段字节，audio/wav，Accept-Ranges: bytes，Content-Length，206 附 Content-Range；Cache-Control: no-store|缺 course_id 422，课与媒体不匹配 403 MEDIA_FORBIDDEN，资源缺失 404，失效 410，范围非法 416 并附 Content-Range: bytes */总字节数|

```json
{"course_id":"c1","revision_id":"r1","title":"示范作业","credit":"示范老师","media_id":"m1","stream_url":"/v1/media/m1/stream?course_id=c1","duration_ms":12000,"phrases":[{"id":"p1","text":"例词甲","lead_ms":0,"start_ms":1000,"entry_ms":1200,"end_ms":4000},{"id":"p2","text":"例词乙","lead_ms":4000,"start_ms":5000,"entry_ms":5100,"end_ms":8000},{"id":"p3","text":"例词丙","lead_ms":8000,"start_ms":9000,"entry_ms":null,"end_ms":12000}]}
```

```json
{"code":"INVALID_BOUNDARY","message":"第二句结束超过音频时长","request_id":"demo-07","field":"phrases[1].end_ms"}
```

错误必有字符串 code、message、request_id，field 可省略。CLI 的 CONFLICT、INVALID_BOUNDARY、UNSUPPORTED_AUDIO、RIGHTS_INVALID 与 STORAGE_ERROR 便于定位失败。HTTP 5xx 保留选句并给重试；无效输入不自动重传。A 每次取流重查许可；B 每次重新播放、切回页面时重查课程，收到 410 清空播放器。已传到设备的字节无法回收。学员原声始终与服务器取流分离。

## A → C：离线 Python 调用

```python
def inspect_lesson(audio_path: Path, phrases: list[dict]) -> dict: ...
```

A 传绝对私有文件路径与上述 phrases；C 只读文件，不写库、不修改输入、不联网。成功返回如下结构，整数为实测值；A 对照时长发布原文件，不做改变零点的转码。

```json
{"schema_version":1,"duration_ms":12000,"sample_rate_hz":16000,"channels":1,"annotation_source":"human_reviewed","phrase_count":3}
```

annotation_source 描述已复核导入流程中的标注来源；许可与复核人由 A 验证。C 抛出 AudioValidationError，提供 code、message、可空 field，由 A 转成 CLI 错误对象。读盘故障走 STORAGE_ERROR。C 单测有效、损坏、越界、空入点；A 用相同返回值及异常桩测发布原子性。B 对正常、410、拒麦、网络失败各保留一组用例。更改字段、路径、单位或错误语义先开 Issue 给出原因、旧新样例和受影响调用方；A 更新本文件与夹具，B/C 确认后同一轮合并，编号写入决策日志。

### 演示许可台账与失败复测

A 的启动说明指定私有运行目录，目录中 grants.json 按 grant_ref 查询，每条记录含字符串 credit、scope（本轮取 local_demo）、expires_at、reviewer_id 以及由 C 核对并登记的 audio_sha256。原始凭证仅在受控位置保存。A 验证媒体哈希、署名、复核人及用途一致，清单到期时间不得晚于授权期限；任一不符即返回 RIGHTS_INVALID。对给定课程执行撤回后，先重开链接，再直接请求旧的 Range URL，最后重启服务重复两步，四次请求均应得到 410。
