# 戏韵 AI 学伴

本机练唱应用。用户从预制曲库选曲，跟随滚动歌词录唱或导入录音，查看逐句音高分、两条波形回听和 AI 声乐练法。

## 启动

在 Windows 上从项目根目录运行：

```powershell
.\tools\start.ps1
```

访问[练唱首页](http://127.0.0.1:5173/)。已配置的示范曲目是《我不难过》。首次克隆后，需要准备自己的音频、歌词时间轴及模型文件。[曲库和模型配置](docs/曲库与模型.md)

只需启动基础前端及曲库时可运行 `py -3.12 -X utf8 -m server --port 5173`。

## 目录

| 目录 | 用途 |
| --- | --- |
| `frontend/studio/` | 选曲、歌词跟唱和逐句点评 |
| `frontend/audio/` | 浏览器录音、解码和基础声学特征 |
| `server/` | 本机 HTTP、曲库 API、CREPE 和 Qwen3 请求 |
| `library/` | 曲库配置和本机原唱、伴奏 |
| `tools/` | 曲目预处理和 Windows 启动脚本 |
| `tests/` | HTTP 和评分回归 |
| `docs/` | 当前产品、部署和验收说明 |
| `legacy/` | 旧平台、双轨原型及早期研究源码 |

模型、密钥、录音、生成数据和曲库音频用本机目录管理。访问控制、模型使用范围以及验证用例见[项目文档](docs/README.md)。

## 测试

```powershell
py -3.12 -X utf8 -m unittest discover -s tests
node --test tests/scoring.test.mjs
```
