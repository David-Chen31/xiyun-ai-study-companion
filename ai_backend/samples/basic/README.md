# 基础素材包 basic-audio-1

按 A 的最新安排，C 先自行寻找基础素材，独立定义临时参数和返回格式，跑通检查能力，之后再与 A 统一接口。此包用来完成这个阶段，不要求先拿到教师真实课包。

## 素材与来源

| 输入 | 来源 | 实测参数 | 用途 |
|---|---|---|---|
| `jfk-speech.wav` | OpenAI Whisper 仓库 `tests/jfk.flac` 的 PCM16 WAV 转换版 | 485100 帧、44100 Hz、双声道、16-bit、11000 ms | 公开语音的参数与三时间窗检查 |
| `pluck-pcm16.wav` | CPython 3.13 `Lib/test/audiodata/pluck-pcm16.wav`，原文件 | 3307 帧、11025 Hz、双声道、16-bit、299 ms | 短音效与不足一毫秒的取整 |
| `../demo_12s.wav` | 本项目 `make_samples.py` 生成 | 192000 帧、16000 Hz、单声道、16-bit、12000 ms | 三句合成样例，空入点 |
| `pluck-pcm8.wav` | CPython 3.13 同目录 8-bit 原文件 | 11025 Hz、双声道、8-bit | 不支持格式的反例，预期 415 |

公开源文件的位置、不可变 Git blob 编号、SHA-256、转换说明和期望参数都在 [manifest.json](manifest.json)。Whisper 仓库所附 MIT 文本保存在 [WHISPER_LICENSE.txt](WHISPER_LICENSE.txt)；CPython 所附许可证及历史条款保存在 [CPYTHON_LICENSE.txt](CPYTHON_LICENSE.txt)。分发时保留这些来源和许可文件。

下载日期：2026-11-21。Whisper 源文件为 24-bit FLAC，制备时使用 soundfile 0.14.0 解码到 int16，再以 16-bit PCM WAV 保存，采样率、声道数、帧数不变。该转换发生在素材准备阶段，运行 C 服务与验证命令不需要 soundfile、numpy 或其他第三方包。服务器本身仍只接受契约范围内的 16-bit PCM WAV，并未增加 FLAC 格式支持。

语音的 0–3600、3600–7300、7300–11000 毫秒是人工设置的技术窗口；不是语音识别结果、老师复核句界或真实入点金标。入点值只用于覆盖数值边界。当前不评估唱词正确性、节奏、音高或自动分句。

## 一条命令重放

在项目根目录运行：

```powershell
python -X utf8 -m ai_backend.check_basics
```

程序先校验素材 SHA-256，自动在回环地址的空闲端口启动 C 服务，通过真实 HTTP 发送 12 个请求，保存结果后关闭自己启动的服务。不影响你已经运行的 8300 服务。

也可以验证一个已运行的 C 服务：

```powershell
$env:AI_BACKEND_TOKEN = "与服务端一致的内部令牌"
python -X utf8 -m ai_backend.check_basics --url http://127.0.0.1:8300
```

预期 `12/12`，进程退出码 0；报告默认写到 `ai_backend/reports/basic_http_results.json`。任何用例不符合预期则退出码 1。401、400、415、422 在相应负例中是正确结果。

前两份素材来自公开仓库测试文件；这个包验证技术能力，不证明它适合真实戏曲教学。A 的业务流程、教师专业复核和后续研究项仍各自验收。
