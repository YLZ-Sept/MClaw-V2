# 插件使用指南（通过 Agent 聊天）

> 本文面向**普通用户**：如何在不打开插件页面的情况下，通过聊天让 Agent 帮你使用这些插件。
> 开发者写插件请参考 [plugin-context-cheatsheet.md](./plugin-context-cheatsheet.md)。

---

## 一句话总原则

**直接用自然语言说你要做什么，Agent 会自动识别并调用对应插件**，不用记任何命令。

每个插件都带一份 `SKILL.md`（写明「是什么 / 何时用 / 工具名」），Agent 根据你的需求自动匹配。想更稳可以**点名插件**（例如「用通义生图……」），但不强求。

---

## 前置：配好 API Key（最关键）

插件分两类，取决于它需不需要单独调外部厂商：

### 需要单独配厂商 Key（生成类）

| 插件 | 中文名 | 需要的 Key |
|---|---|---|
| `tongyi-image` | 通义生图 | 阿里云百炼 `DASHSCOPE_API_KEY` |
| `happyhorse-video` | 快乐马工作室 | 阿里云百炼 `DASHSCOPE_API_KEY` |
| `clip-sense` | 智剪工坊 | 阿里云百炼 `DASHSCOPE_API_KEY` |
| `avatar-studio` | 数字人工作室 | 阿里云百炼 `DASHSCOPE_API_KEY`（或 `AVATAR_STUDIO_DASHSCOPE_API_KEY`） |
| `media-post` | 发布物料工坊 | 阿里云百炼 `DASHSCOPE_API_KEY` |
| `subtitle-craft` | 字幕工坊 | 阿里云百炼 `DASHSCOPE_API_KEY` |
| `seedance-video` | 即梦工作室 | 火山引擎 `ARK_API_KEY` |
| `manga-studio` | 漫剧工作室 | `ARK_API_KEY` / `DASHSCOPE_API_KEY` / `RUNNINGHUB_API_KEY`（按功能用） |
| `idea-research` | 选题研析室 | `DASHSCOPE_API_KEY` / `MOONSHOT_API_KEY` / `YOUTUBE_API_KEY`（按功能用） |
| `chanjing-dp` | 数字人（TTS） | `CHANJING_APP_ID` / `CHANJING_SECRET_KEY`（无 UI 页面） |

### 复用主 LLM（无需单独 Key）

办公 / 策略 / 财经类，直接用你在设置里配好的主大模型：

- `excel-maker` Excel 报表助手
- `word-maker` Word 文档助手
- `ppt-maker` PPT 制作助手
- `fin-pulse` 财经脉动
- `finance-auto` 财务自动化
- `footage-gate` 成片质量门
- `media-strategy` 融媒智策
- `omni-post` 全媒发布
- `ecommerce-image` 电商素材小助理

### 在哪配 Key

**通过 Agent 聊天配置**（或直接调 API）。直接跟 Agent 说「把 DashScope key 配到通义生图」，Agent 会写进插件自己的配置库。

> 说明：当前部署里没有填 key 的 UI——「工作台」的「配置」按钮是宿主级配置（对插件 key 是空的），插件自带的「设置」页其 `ui/` 目录未部署到运行时。所以配 key 走 Agent 聊天或 API 最直接。

没配 Key 的典型表现：请求报 502、Agent 提示「去 Settings 配置」。

---

## 插件能力一览

| 插件 | 中文名 | 能做什么 |
|---|---|---|
| `tongyi-image` | 通义生图 | 文生图 / 图生图 / 换风格 / 补背景 / 扩画面 / 涂鸦成图 / 电商套图 |
| `seedance-video` | 即梦工作室 | 文生视频 / 图生视频（字节即梦） |
| `avatar-studio` | 数字人工作室 | 照片说话 / 视频换嘴 / 换脸 / 数字人合成 |
| `clip-sense` | 智剪工坊 | 视频高光提取 / 静音精剪 / 拆条 / 口播精编 |
| `ecommerce-image` | 电商素材小助理 | 电商主图 / 详情页 / 海报 / 视频（19 场景） |
| `manga-studio` | 漫剧工作室 | 漫画 / 漫剧生成 |
| `happyhorse-video` | 快乐马工作室 | 视频生成 |
| `footage-gate` | 成片质量门 | 成片质检 |
| `subtitle-craft` | 字幕工坊 | 字幕生成 / 翻译 |
| `excel-maker` | Excel 报表助手 | 一键生成 Excel 报表 |
| `word-maker` | Word 文档助手 | 一键生成 Word 文档 |
| `ppt-maker` | PPT 制作助手 | 一键生成 PPT |
| `fin-pulse` | 财经脉动 | 财经数据 / 分析 |
| `finance-auto` | 财务自动化 | 报表 / 财务流程自动化 |
| `idea-research` | 选题研析室 | 选题研究 |
| `media-strategy` | 融媒智策 | 媒体策略 |
| `media-post` | 发布物料工坊 | 发布物料生成 |
| `omni-post` | 全媒发布 | 多平台发布 |

---

## 示例话术

给「做什么 + 关键参数」即可：

- **生图**：「用通义生图，画一张中秋月饼礼盒海报，中式国潮风，竖版 1024×1536」
- **图生图 / 换风格**：把图发过去 +「把这张图换成水墨风格」
- **电商套图**：「给这个产品出一套白底电商主图，多场景」
- **视频**：「用即梦生成一段 5 秒的江南水乡航拍视频」
- **数字人**：「用这张照片做个数字人口播视频，文案是……」
- **剪辑**：「把这段视频里的静音片段去掉，只留高光」
- **字幕**：「给这个视频生成中文字幕」
- **文档**：「根据这份数据生成一个 Excel 报表」「把这份文档做成 PPT」

---

## 结果怎么回

这些插件基本都是**异步任务**（提交 → 轮询 → 完成）：

- 生成完自动落盘（图片/视频/文档存到插件自己的 data 目录）。
- Agent 会在聊天里把结果或文件给你。
- 图/视频的临时 URL 通常 24h 过期，所以默认**自动下载到本地**再给你。

---

## 排查小抄

- 想知道有哪些能力：直接问「你现在能做哪些内容生成 / 列出可用技能」。
- 报错先看是不是**没配 Key**（502 / "去 Settings 配置"），再怀疑别的。
- 敏感词 / 内容审核命中会直接失败且不可重试——换个说法再试。
- 长任务排队可能等几分钟，属于正常，不会卡住聊天。

---

*最后更新：2026-08-19*
