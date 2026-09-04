---
name: mclaw/skills@douyin-comments
description: "Douyin (TikTok China) comment reading & auto-reply skill for 米贝科技 official account. Reads comments under videos via CDP-connected Edge browser, generates grounded replies with sensitive-topic filtering (price/competitor/politics/abuse), and sends after human confirmation. Use when user wants to read Douyin comments, generate reply suggestions, reply to commenters, or check their video list."
license: MIT
metadata:
  author: mclaw
  version: "1.0.0"
---

# 抖音评论读取与回复工具

自动读取抖音视频评论、智能生成回复（敏感话题过滤）、人工确认后发送，适用于米贝科技官方号（抖音号 44264185979）的日常评论运营。

## 适用场景

- **运营辅助**：查看用户反馈、收集选题灵感、跟进互动数据
- **客服转化**：回复咨询、引导私信、留资转化
- **账号日常**：统一回复风格，提高评论区互动率

## 账号与风格

- **账号**：米贝科技官方号（抖音号 44264185979）
- **回复风格**：亲切接地气，简短有力（≤50 字），用「你」不用「您」
- **审核模式**：AI 生成 + 人工确认（推荐，更稳妥）

## 敏感规则（不可违背）

| 类型 | 处理方式 |
|------|---------|
| 价格/报价类 | 不直接报价，引导私信沟通 |
| 竞品相关 | 不踩不捧，转回自身优势（云南本地化服务） |
| 敏感话题（监管/政治等） | 不回复，直接跳过 |
| 辱骂/恶意评论 | 不回复，可标记删除 |

## 技术方案（与 Mclaw 项目结合点）

- **浏览器控制**：CDP（Chrome DevTools Protocol）连接 Edge，端口 9222，用户数据目录 `~/.mclaw/data/douyin-browser/`
  - 复用 Mclaw 后端自带依赖（`websockets`、`httpx`），**无需安装 Playwright**
  - 登录态持久化：扫码一次，之后 `launch-browser` 直接复用
- **回复生成**：基于规则的模板匹配 + 敏感词检测（`reply_generator.py`）
- **人工确认**：交互式命令行逐条 y/e/n/q
- **JSON 输出**：`read/generate/list-videos` 支持 `--json`，供 Agent 工具链解析

## 快速开始

### 1. 启动浏览器并登录（仅首次需要扫码）

```bash
python scripts/douyin_comments.py launch-browser
# 浏览器弹出后扫码登录抖音网页版
```

> ⚠️ 需要有图形界面的电脑。若 Edge 已在运行且端口被占用，先 `--kill` 再启动。

### 2. 检查连接状态

```bash
python scripts/douyin_comments.py status
```

### 3. 读取指定视频的评论

```bash
python scripts/douyin_comments.py read <视频URL> --max 30
```

### 4. 生成回复建议（不发送）

```bash
python scripts/douyin_comments.py generate <视频URL> --max 20 --topic 等保测评
```

### 5. 逐条回复（人工确认）

```bash
python scripts/douyin_comments.py reply <视频URL> --max 10 --topic MClaw智能体
```

交互选项：`y` 发送 / `e` 编辑 / `n` 跳过 / `q` 退出

### 6. 查看账号视频列表

```bash
python scripts/douyin_comments.py list-videos --max 20
```

## 工作流程

```
读取评论 → 敏感词检测 → 生成回复 → 人工确认/编辑 → 点击发送
        ↓（政治/辱骂类敏感）
      自动跳过
```

## 业务主题覆盖

| 主题 | 关键词 |
|------|--------|
| 等保测评 | 等保、等级保护、等保2.0、等保二级/三级 |
| 数据安全 | 数据安全、数据治理、数据泄露、数据合规 |
| 勒索病毒 | 勒索、病毒、挖矿、黑客、攻击 |
| 安全运维 | 安全运维、运维服务、应急响应、驻场 |
| CCRC认证 | CCRC、信息安全服务资质、资质认证 |
| MClaw智能体 | MClaw、智能体、AI Agent、AI办公、自动化 |
| 合作咨询 | 合作、加盟、代理、商务、联系方式 |

## 脚本说明

| 文件 | 作用 |
|------|------|
| `scripts/douyin_comments.py` | 主脚本：launch/status/read/generate/reply/list-videos |
| `scripts/reply_generator.py` | 回复生成器：敏感词过滤 + 模板匹配，支持 `--batch` 批量模式 |
| `scripts/cdp_client.py` | CDP 协议客户端（websockets + httpx，复用 Mclaw 依赖） |

## 环境变量（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOUYIN_CDP_PORT` | `9222` | CDP 调试端口 |
| `DOUYIN_USER_DATA` | `~/.mclaw/data/douyin-browser` | 浏览器用户数据目录（登录态） |
| `DOUYIN_REPLY_INTERVAL` | `2.0` | 每条回复间隔秒数（防风控） |

## 注意事项

1. **首次使用需要扫码登录**，登录状态保存在用户数据目录
2. **评论操作频率不要过快**，每条回复间隔 2-3 秒，避免触发风控
3. **政治/辱骂类敏感内容自动跳过**，绝不发送
4. **价格类问题绝不直接报价**，统一引导私信
5. **回复前务必人工确认**，AI 生成的内容可能不准确或不合时宜
6. 抖音网页版 DOM 结构可能变化，如遇到读取异常请调整 `extract_comments` 中的选择器
7. **操作执行域**：浏览器在本机/Mclaw 宿主所在主机上启动；用户若在手机/远程设备操作，需确认目标浏览器在可访问的主机上

## 与 E:\桌面\抖音 版本的区别

| 项 | 原版 | 本技能版 |
|----|------|---------|
| 浏览器控制 | Playwright + Chromium | CDP + Edge（复用 Mclaw 依赖，无新增安装） |
| 回复生成 | 模板匹配 | 同源，增加 `--batch` stdin JSON 模式 |
| Agent 集成 | 无 | read/generate/list-videos 支持 `--json`，供 Agent 解析 |
| 环境 | 独立 venv | 直接用 Mclaw 后端/Agent venv |
