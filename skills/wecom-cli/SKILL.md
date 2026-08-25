---
name: mclaw/skills@wecom-cli
description: "WeCom (Enterprise WeChat) CLI - official open-source CLI tool from WeCom. Covers 14 service domains: Contacts, Todos, Meetings, Messages, Chats, Schedules, Documents, Sheets, Smartsheets, Smartpages, Mail, Disk, Media, Auth. Built in Rust for macOS/Linux/Windows. Use when user wants to operate WeCom resources."
license: MIT
metadata:
  author: WecomTeam
  version: "1.1.0"
---

# 企业微信 CLI (wecom-cli)

企业微信开放平台官方命令行工具 — 让人类和 AI Agent 都能在终端中操作企业微信。

> 官方 GitHub: https://github.com/WecomTeam/wecom-cli
> 官方帮助: https://open.work.weixin.qq.com/help2/pc/21676

## 安装

```bash
# 安装 CLI
npm install -g @wecom/cli

# 配置凭证（交互式，扫码，仅需一次）
wecom-cli auth init

# 查看授权状态
wecom-cli auth show
```

> 扫码：运行 `wecom-cli auth init` 后终端显示二维码，用【企业微信】App 扫码并确认授权即可。
> 无人值守/无浏览器场景：`wecom-cli auth init --noninteractive --no-browser --output-qrcode qr.png` 输出二维码 PNG。
> 手动输入凭证：`wecom-cli auth init --manual`（输入智能机器人 Bot ID 和 Secret）。

### 前置条件

- 支持平台：macOS (x64/arm64)、Linux (x64/arm64) 及 Windows (x64)
- Node.js >= 18
- 企业微信账号（扫码授权）
- （可选）智能机器人 Bot ID 和 Secret

## 功能范围

覆盖企业微信核心业务品类（14 个服务域）：

| 服务域 | 能力 |
|--------|------|
| 👤 contact | 通讯录成员搜索（`users search`） |
| 📄 doc | 文档创建/导入/搜索/内容读写/成员/重命名/权限 |
| 📊 sheet | 在线表格：创建工作表/读取/行/子表/范围 |
| 🧮 smartsheet | 智能表格：创建/字段/记录/视图/图表/子表 |
| 📰 smartpage | 智能页面：创建/markdown 导入/区块/数据库 |
| ✅ todo | 待办创建/读取/更新/完成/删除 |
| 📅 calendar | 日程增删改查/搜索/忙闲 |
| 🎥 meeting | 会议创建/查询/搜索/更新/取消/会议室 |
| 💬 message | 发送文本消息（单聊/群聊） |
| 🗨️ chat | 会话列表/消息记录拉取 |
| 📧 mail | 邮件发送/搜索/读取 |
| 💾 disk | 微盘文件/文件夹 |
| 📎 media | 媒体文件上传/下载 |
| 🔐 auth | 授权管理（init/show） |

## 常用命令

```bash
# 通讯录成员搜索
wecom-cli contact users search --keywords 杨
wecom-cli contact users search --search-mode list            # 全量列表模式

# 待办
wecom-cli todo list                                          # 待办列表
wecom-cli todo create --items '[{"title":"周报"}]'            # 批量创建（最多 20 条）

# 文档
wecom-cli doc create --doc-name "项目计划" --doc-type doc     # doc_type: doc/sheet/smartsheet

# 会议 / 日程
wecom-cli meeting list                                       # 默认当前到 30 天后
wecom-cli calendar schedules list

# 发送文本消息
wecom-cli message send --chat-id <userid> --msg-type text --text '{"content":"Hello"}'
```

## 通用选项

所有命令支持：
- `--json <JSON>`：传原始请求体
- `--set <path=val>`：深层路径覆盖
- `--dry-run`：仅本地校验，不实际发送
- `--schema` / `--doc`：查看接口 schema / 文档
- `-o <file>` / `--output-dir <dir>`：响应写入文件/目录

## 安全规则

- 写入/删除操作前确认用户意图
- 不输出密钥到终端明文
- 凭证通过 `auth init` 交互式初始化，安全存储
- 机器人调用以机器人身份代授权用户执行；只能写入/修改机器人自己创建或拥有的数据

## 预置脚本

### scripts/setup.py
安装并初始化 wecom-cli：

```bash
python3 scripts/setup.py
```

### scripts/wecom_quick.py
常用操作快捷封装：

```bash
python3 scripts/wecom_quick.py send-msg --to <userid> --content "Hello"
python3 scripts/wecom_quick.py contacts --keywords 杨
python3 scripts/wecom_quick.py create-doc --title "新文档"
python3 scripts/wecom_quick.py schedule
```