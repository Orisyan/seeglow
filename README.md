# 拾光 SeeGlow

> 让长视频，一眼见光。粘贴一个B站链接，得到一份结构化的中文总结笔记。

拾光（SeeGlow）是一个**B站长视频总结工具**：自动获取视频字幕，无字幕时让多模态大模型直接听音频，生成包含「一句话总结 / 核心要点 / 内容时间线 / 金句摘录」的 Markdown 笔记。

## 特性

- **一个模型干两份活**：只需一个支持音频输入的多模态模型（如 Qwen2.5-Omni、GPT-4o-audio），既能直接听视频写总结，也能处理字幕文本
- **纯 AI，零本地转写**：无字幕视频由多模态模型直接听音频理解内容，无需任何本地语音引擎
- **超长视频不慌**：音频自动切段逐段理解后汇总，几小时的视频也能完整总结
- **本地文件也能总结**：拖入 mp4 / mp3 / 播客 / 录音，AI 直接聆听出笔记
- **五种总结风格**：通用笔记 / ⚡速学模式（必会知识点+自测五题）/ 学习笔记（含术语表）/ 教程步骤 / 会议纪要 / 轻松杂谈
- **批量总结**：粘贴合集或收藏夹链接（或合集内任一视频），勾选后批量出笔记并生成目录
- **弹幕高能时间轴**：总结附弹幕密度热力条，一眼看到全片高能点，点击跳转
- **时间戳一键跳转**：笔记里的 `[mm:ss]` 点击直达B站对应时间点
- **导出增强**：Markdown / 思维导图 .opml / Anki 闪卡 / SRT·VTT 字幕
- **一键改写文案**：笔记秒变小红书笔记、公众号文章、微博文案、知乎回答
- **追更检查**：关注 UP 主，一键检查新视频并批量总结（需 SESSDATA）
- **AI 视频问答**：基于视频真实内容回答追问，引用时间点
- **浏览器插件**：B站视频页一键发送到拾光（见「浏览器插件」文件夹）
- **双入口**：本地网页界面 + 命令行
- **纯本机运行**：总结结果保存在本地 `拾光/` 目录，不上传任何数据
- **OpenAI 兼容**：支持硅基流动、OpenAI 等任何兼容 API，服务商预设一键填好

## 安装

```bash
# 需要 Python 3.9+
pip install -r requirements.txt
```


## 快速开始

### 网页版（推荐）

```bash
python -m seeglow --web
# 浏览器打开 http://127.0.0.1:8765
```

1. 首次使用点击右上角「设置」，填入 API 地址、API Key、模型名
2. 粘贴B站视频链接 → 点击「开始总结」
3. 实时查看进度，完成后在线阅读、复制或下载 Markdown

### 命令行

```bash
python -m seeglow "https://www.bilibili.com/video/BVxxxx"
python -m seeglow BV1GJ411x7h7
python -m seeglow "https://b23.tv/xxxx"

# 多P视频
python -m seeglow "BVxxxx" --p 3            # 只总结第3个分P
python -m seeglow "BVxxxx" --all-pages      # 总结全部分P，合并为一份笔记
```

> 网页版粘贴多P链接会自动弹出分P列表，点选即可。

### 封装版 EXE（爱发电获取）

免装 Python、双击即用的封装版通过 [爱发电](https://afdian.com/a/Orisyan) 订阅获取，包含图形界面、授权管理与自动更新。

技术用户也可自行打包：`pip install pyinstaller` 后执行 `.\build_exe.ps1`（生成命令行版）。

支持：完整链接（含 `?p=` 分P）、BV号、av号、b23.tv 短链。

## 配置

配置保存在项目根目录 `config.json`（网页版设置页可改），也可用环境变量 `SEELOW_XXX` 覆盖：

| 字段 | 说明 | 默认 |
|---|---|---|
| `provider` | 服务商预设：siliconflow / openai / groq / custom | 空 |
| `api_base` | OpenAI 兼容 API 地址（总结与转写共用） | `https://api.siliconflow.cn/v1` |
| `api_key` | API Key（一个 Key 干两份活） | 空 |
| `model` | 模型名称（需支持音频输入，如 `Qwen/Qwen2.5-Omni-7B`） | `Qwen/Qwen2.5-Omni-7B` |
| `temperature` | 采样温度 0~1 | `0.3` |
| `sessdata` | B站 Cookie SESSDATA（可选） | 空 |
| `output_dir` | 总结输出目录 | `./拾光` |

**关于 SESSDATA**：部分视频的AI字幕和高清音频流需要登录态才能获取。在浏览器登录B站后复制 SESSDATA Cookie 填入即可；不填也能正常工作（无字幕视频由 AI 直接听音频）。

## 工作流程

```
解析链接 → 获取视频信息 → 尝试B站字幕 ──有──→ 文本总结
                              │
                              无
                              ↓
                       下载音频(DASH)
                              ↓
              多模态模型直听音频（切段逐段理解 → 汇总）
                              ↓
                保存 Markdown 到 拾光/ 目录
```

**推荐模型预设**（设置页选服务商即自动填好，一个 Key 一个模型全搞定）：

| 服务商 | API 地址 | 模型 |
|---|---|---|
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-Omni-7B` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-audio-preview` |

> 注意：纯文本模型（如 DeepSeek 官方 API）不能听音频；无字幕视频会直接报错。想全自动出稿，请选支持音频输入的模型。

## 常见问题

**Q: 提示"AI 直听音频失败"？**
说明当前配置的模型不支持音频输入（如 DeepSeek 官方 API 的纯文本模型）。请在设置里换成支持音频的模型（推荐硅基流动的 `Qwen/Qwen3-Omni-30B-A3B-Instruct`）；或配置 SESSDATA 让工具优先使用B站字幕。

**Q: 音频下载失败 / 拿不到字幕？**
给配置加上 SESSDATA；或检查视频是否为付费/专属内容（暂不支持）。

**Q: 打包成 exe 发布？**
源码基于 MIT 协议免费开源。如需封装版，可用 PyInstaller：

```bash
pip install pyinstaller
pyinstaller -n SeeGlow --noconsole --collect-all seeglow ^
  --add-data "seeglow/static;seeglow/static" run_web.py
```

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。当前版本 **v0.4.0**：时间戳跳转、本地文件总结、风格模板、合集/收藏夹批量、弹幕高能时间轴、思维导图/闪卡导出、追更、浏览器插件。

## 免责声明

本项目仅供学习交流，请尊重UP主版权，勿用于商业搬运。总结由AI生成，可能存在偏差，请以原视频为准。

## 支持作者

拾光采用「源码免费 + 封装版订阅」模式：

- **源码永久免费**：MIT 协议，可自由使用、修改、分发
- 如果这个项目帮到了你，欢迎到 [爱发电](https://afdian.com/a/Orisyan) 支持一下
- **封装版**（免装 Python、双击即用）与持续更新通过爱发电订阅获取：https://afdian.com/a/Orisyan

## License

MIT
