#!/usr/bin/env python3
"""
抖音评论读取与回复工具（Mclaw 技能版）

基于 CDP (Chrome DevTools Protocol) 控制浏览器，无需额外安装 Playwright，
复用 Mclaw 项目自带依赖（websockets / httpx）。

用法:
  python douyin_comments.py launch-browser          # 启动浏览器（Edge + CDP 端口，扫码登录）
  python douyin_comments.py status                  # 检查浏览器连接状态
  python douyin_comments.py read <URL> [--max 30]   # 读取评论
  python douyin_comments.py generate <URL> [--topic 等保测评]   # 生成回复建议（不发送）
  python douyin_comments.py reply <URL>             # 逐条回复（人工确认）
  python douyin_comments.py list-videos             # 查看账号已发布的视频列表
  python douyin_comments.py launch-browser --kill   # 关闭之前启动的浏览器

输出说明:
  - read / generate / list-videos 默认终端友好文本；加 --json 输出 JSON（供 Agent 解析）
"""

import sys
import os
import json
import re
import argparse
import asyncio
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp_client import CDPClient, list_targets, ping
from reply_generator import ReplyGenerator

# ============================================================
# 常量
# ============================================================
DOUYIN_URL = "https://www.douyin.com"
CDP_PORT = int(os.environ.get("DOUYIN_CDP_PORT", "9222"))
USER_DATA_DIR = Path(os.environ.get(
    "DOUYIN_USER_DATA",
    os.path.expanduser("~/.mclaw/data/douyin-browser"),
))
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Edge 浏览器路径（优先用户安装路径，兜底系统路径）
EDGE_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
]
EDGE_PATH = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), "msedge")

REPLY_INTERVAL = float(os.environ.get("DOUYIN_REPLY_INTERVAL", "2.0"))  # 风控间隔秒


def log(msg: str) -> None:
    print(msg, flush=True)


# ============================================================
# 浏览器连接
# ============================================================
async def get_client() -> CDPClient:
    """连接 CDP 浏览器，返回第一个可用页面的客户端"""
    client = CDPClient(port=CDP_PORT)
    await client.connect()
    return client


def cmd_status(args) -> None:
    """检查浏览器连接状态"""
    try:
        targets = asyncio.run(list_targets(port=CDP_PORT))
        log(f"✅ 浏览器已连接，共 {len(targets)} 个 target：")
        for t in targets[:10]:
            title = (t.get("title") or "无标题")[:60]
            url = (t.get("url") or "")[:80]
            log(f"  [{t.get('type', '?')}] {title}")
            log(f"       {url}")
        log(f"\n用户数据目录: {USER_DATA_DIR}")
    except Exception as e:
        log(f"❌ 未检测到浏览器 (端口 {CDP_PORT}): {e}")
        log(f"请先运行: python douyin_comments.py launch-browser")


def cmd_launch_browser(args) -> None:
    """启动 Edge 浏览器（带 CDP 端口，用于扫码登录）"""
    if getattr(args, "kill", False):
        log("🛑 关闭浏览器进程...")
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "msedge.exe"],
                capture_output=True,
            )
            log("   已发送关闭指令（会关闭所有 Edge 窗口，请确认无其他工作窗口）")
        except Exception as e:
            log(f"   ❌ 关闭失败: {e}")
        return

    # 检查是否已有浏览器在监听
    if asyncio.run(ping(port=CDP_PORT)):
        log(f"ℹ️  端口 {CDP_PORT} 已有浏览器在监听，直接复用即可")
        log("   运行: python douyin_comments.py status")
        return

    log("🚀 启动 Edge 浏览器（CDP 调试模式）...")
    log(f"   用户数据目录: {USER_DATA_DIR}")
    log(f"   CDP 端口: {CDP_PORT}")

    cmd = [
        EDGE_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={str(USER_DATA_DIR)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2",
        DOUYIN_URL,
    ]
    try:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
    except AttributeError:
        subprocess.Popen(cmd, close_fds=True)

    log("\n✅ 浏览器已启动！请在窗口中扫码登录抖音网页版")
    log("   登录完成后，在另一个终端运行: python douyin_comments.py status")
    log("   然后: python douyin_comments.py list-videos 查看视频列表")


# ============================================================
# 页面操作
# ============================================================
async def open_video(client: CDPClient, url: str) -> None:
    """打开视频页面"""
    log(f"📂 打开视频: {url}")
    await client.navigate(url)
    try:
        await client.wait_for_load(timeout=30)
    except TimeoutError:
        log("   ⚠️ 页面加载超时，继续尝试...")
    await asyncio.sleep(3)


async def scroll_and_load_comments(client: CDPClient, target_count: int = 30) -> int:
    """滚动加载更多评论，返回最终加载条数"""
    log(f"📜 加载评论中 (目标: {target_count}条)...")
    await client.evaluate("window.scrollTo(0, 600)")
    await asyncio.sleep(2)

    loaded = 0
    for i in range(15):
        count = await client.evaluate(
            "document.querySelectorAll('[data-e2e=\"comment-item\"], .comment-item').length"
        ) or 0

        log(f"  第{i+1}轮: 已加载 {count} 条")

        if count >= target_count or count == loaded:
            loaded = count
            break

        loaded = count
        # 评论列表滚动 / 页面滚动
        await client.evaluate("""
            () => {
                const list = document.querySelector('[data-e2e="comment-list"], .comment-list');
                if (list) { list.scrollTop = list.scrollHeight; }
                else { window.scrollBy(0, 600); }
            }
        """)
        await asyncio.sleep(2)

    log(f"✅ 共加载 {loaded} 条评论")
    return loaded


async def extract_comments(client: CDPClient) -> List[Dict]:
    """提取页面上的评论"""
    comments = await client.evaluate("""
        () => {
            const selectors = [
                '[data-e2e="comment-item"]',
                '.comment-item',
                'div[data-e2e="feed-comment"]',
            ];
            let items = [];
            for (const sel of selectors) {
                const found = document.querySelectorAll(sel);
                if (found.length > 0) { items = Array.from(found); break; }
            }

            const results = [];
            items.forEach((item, index) => {
                const q = (sels) => {
                    for (const s of sels) {
                        const el = item.querySelector(s);
                        if (el && el.textContent.trim()) return el.textContent.trim();
                    }
                    return '';
                };
                const user = q([
                    'a[href*="/user/"]', '[data-e2e="comment-user-name"]', '.comment-user-name',
                    '.user-name', 'a[class*="name"]',
                ]);
                const text = q([
                    '.FduGc_lz', '[data-e2e="comment-content"]', '.comment-content',
                    '.comment-text', 'p[class*="content"]', 'div[class*="content"]',
                ]);
                let likes = 0;
                const likeText = q([
                    '.VpA2NKl1', '[data-e2e="comment-like-count"]', '.like-count', '.digg-count',
                ]);
                const m = likeText.match(/([\\d.]+)万/);
                if (m) likes = parseFloat(m[1]) * 10000;
                else { const m2 = likeText.match(/\\d+/); if (m2) likes = parseInt(m2[0]); }
                const time = q([
                    '.VAQA49VP', '[data-e2e="comment-time"]', '.comment-time', '.time-text',
                ]);
                let hasReplyBtn = true;
                results.push({ index, user, text, likes, time, hasReplyBtn });
            });
            return results;
        }
    """)
    return comments or []


async def reply_to_comment(client: CDPClient, comment_index: int, reply_text: str) -> bool:
    """
    回复指定索引的评论
    流程: 点击回复按钮 → 聚焦输入框 → 清空重填 → 点发送 / 按 Enter
    """
    # 1. 点击该评论的回复按钮（闭包内嵌索引）
    clicked = await client.evaluate(f"""
        (() => {{
            const index = {comment_index};
            const items = document.querySelectorAll('[data-e2e="comment-item"], .comment-item');
            if (!items[index]) return false;
            const item = items[index];
            // 新版：文本"回复"按钮（LJU9cDNW / tFq3uJx3），无独立 e2e 标记
            const btn = item.querySelector(
                '.LJU9cDNW, .tFq3uJx3, [data-e2e="comment-reply"], .reply-btn, button.reply, .comment-reply'
            ) || Array.from(item.querySelectorAll('div, span')).find(n =>
                (n.textContent || '').trim() === '回复' && n.children.length === 0 && n.offsetParent !== null
            );
            if (btn) {{
                btn.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                btn.click();
                return true;
            }}
            return false;
        }})()
    """)

    if not clicked:
        log("  ⚠️  找不到回复按钮")
        return False

    await asyncio.sleep(1.5)

    # 2. 定位输入框（点击回复按钮后出现；新版为 Draft.js contenteditable）
    input_sel = 'textarea[placeholder*="回复"], textarea[placeholder*="说点什么"], .public-DraftEditor-content, div[contenteditable="true"]'
    # 取最后一个输入框
    focused = await client.evaluate(f"""
        (() => {{
            const inputs = document.querySelectorAll({json.dumps(input_sel)});
            const input = inputs[inputs.length - 1];
            if (!input) return false;
            input.focus();
            input.scrollIntoView({{behavior: 'smooth', block: 'center'}});
            return true;
        }})()
    """)
    if not focused:
        log("  ⚠️  找不到回复输入框")
        return False

    await asyncio.sleep(0.3)

    # 3. 真实插入文本（CDP Input.insertText，触发 Draft.js/React 状态更新，发送按钮才会出现）
    await client.insert_text(reply_text)
    await asyncio.sleep(0.5)

    # 4. 发送：优先点发送按钮（新版为红色箭头图标，位于 .oWdMk9B9 容器），失败则按 Enter
    sent = await client.evaluate("""
        () => {
            // 策略1：输入容器内的红色箭头（新版）
            const container = document.querySelector('.comment-input-inner-container .oWdMk9B9');
            if (container) {
                const spans = container.querySelectorAll('span');
                for (const span of spans) {
                    const svg = span.querySelector('svg');
                    if (svg && span.offsetParent !== null) {
                        const p = svg.querySelector('path[fill="#FE2C55"], path[fill="#fe2c55"]');
                        if (p) { span.click(); return true; }
                    }
                }
            }
            // 策略2：经典选择器
            const sendBtns = document.querySelectorAll(
                '.wchsYBpK, [data-e2e="comment-submit"], .comment-submit, .send-btn, button[type="submit"]'
            );
            for (const btn of sendBtns) {
                if (!btn.disabled && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }
    """)

    if not sent:
        # 找不到发送按钮 → 用 Enter 提交
        await client.press_enter()

    await asyncio.sleep(1.5)
    return True


async def get_my_videos(client: CDPClient, max_count: int = 20) -> List[Dict]:
    """获取账号视频列表"""
    log("📹 获取账号视频列表...")
    await client.navigate(f"{DOUYIN_URL}/user/self")
    try:
        await client.wait_for_load(timeout=30)
    except TimeoutError:
        pass
    await asyncio.sleep(3)

    for _ in range(5):
        await client.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(2)

    videos = await client.evaluate("""
        () => {
            // 抖音新版：作品列表在 [data-e2e="user-post-list"] 内的 li > a[href*="/video/"]
            const links = document.querySelectorAll(
                '[data-e2e="user-post-list"] a[href*="/video/"]'
            );
            const results = [];
            links.forEach((a, i) => {
                const img = a.querySelector('img');
                let title = img ? (img.alt || '') : '';
                title = title.replace(/\\s+/g, ' ').trim().slice(0, 100);
                // 播放量（可选，部分版本有）
                const playEl = a.querySelector('span[class*="play"], .BP1CQkLg');
                const plays = playEl ? playEl.textContent.trim() : '';
                results.push({ index: i, url: a.href, title: title || '无标题', plays: plays });
            });
            return results;
        }
    """)
    return videos or []


# ============================================================
# 命令处理
# ============================================================
def cmd_read(args) -> None:
    """读取视频评论"""
    url = args.url
    max_count = args.max
    json_out = getattr(args, "json", False)

    async def run():
        client = await get_client()
        try:
            await open_video(client, url)
            await scroll_and_load_comments(client, max_count)
            comments = await extract_comments(client)
            comments = comments[:max_count]

            if json_out:
                print(json.dumps(comments, ensure_ascii=False, indent=2))
                return

            log(f"\n📋 共读取到 {len(comments)} 条评论：\n")
            for c in comments:
                mark = "💬可回复" if c["hasReplyBtn"] else "🔒无回复按钮"
                log(f"[{c['index']+1}] 👤 {c['user']} ({mark})")
                log(f"    💬 {c['text'][:100]}")
                log(f"    ❤️ {c['likes']}  ⏰ {c['time']}")
                log("")

            # 保存
            video_id = extract_video_id(url)
            save_comments(video_id, comments)
        finally:
            await client.close()

    asyncio.run(run())


def cmd_generate(args) -> None:
    """生成回复建议（不发送）"""
    url = args.url
    max_count = args.max
    topic = getattr(args, "topic", "")
    json_out = getattr(args, "json", False)

    async def run():
        client = await get_client()
        try:
            await open_video(client, url)
            await scroll_and_load_comments(client, max_count + 10)
            comments = await extract_comments(client)
            valid = [c for c in comments if c["text"] and len(c["text"]) > 1][:max_count]

            gen = ReplyGenerator()
            results = []
            for c in valid:
                r = gen.generate_reply(c["text"], c["user"], topic)
                results.append({
                    "user": c["user"],
                    "text": c["text"],
                    "index": c["index"],
                    "reply": r.reply,
                    "is_sensitive": r.is_sensitive,
                    "sensitive_type": r.sensitive_type,
                    "suggestion": r.suggestion,
                    "confidence": r.confidence,
                })

            if json_out:
                print(json.dumps(results, ensure_ascii=False, indent=2))
                return

            log(f"\n🤖 有效评论 {len(results)} 条，生成回复建议：\n")
            for i, r in enumerate(results):
                log(f"[{i+1}] 👤 {r['user']}")
                log(f"    💬 {r['text']}")
                if r["is_sensitive"]:
                    icon = "🔴" if r["sensitive_type"] in ("political", "abuse") else "🟡"
                    log(f"    {icon} 敏感: {r['sensitive_type']} - {r['suggestion']}")
                if r["reply"]:
                    log(f"    ✨ 回复: {r['reply']}")
                log("")
        finally:
            await client.close()

    asyncio.run(run())


def cmd_reply(args) -> None:
    """逐条回复（人工确认）"""
    url = args.url
    max_count = args.max
    topic = getattr(args, "topic", "")

    log("✍️  回复模式（人工逐条确认）")
    log("  y = 发送   e = 编辑   n = 跳过   q = 退出\n")

    async def run():
        client = await get_client()
        try:
            await open_video(client, url)
            await scroll_and_load_comments(client, max_count + 10)
            comments = await extract_comments(client)
            replyable = [c for c in comments if c["text"] and len(c["text"]) > 1 and c["hasReplyBtn"]][:max_count]
            log(f"📋 可回复评论 {len(replyable)} 条\n")

            if not replyable:
                log("❌ 没有找到可回复的评论")
                return

            gen = ReplyGenerator()
            sent_count = 0

            for i, c in enumerate(replyable):
                r = gen.generate_reply(c["text"], c["user"], topic)

                log(f"─── 第 {i+1}/{len(replyable)} 条 ───")
                log(f"👤 {c['user']}")
                log(f"💬 {c['text']}")

                if r.is_sensitive and r.sensitive_type in ("political", "abuse"):
                    log(f"🔴 敏感 [{r.sensitive_type}]: {r.suggestion}")
                    log(f"   ⏭️  自动跳过\n")
                    continue

                if r.is_sensitive:
                    log(f"🟡 注意 [{r.sensitive_type}]: {r.suggestion}")

                current_reply = r.reply
                log(f"✨ AI回复: {current_reply}")

                while True:
                    choice = input("[y发送 / e编辑 / n跳过 / q退出] > ").strip().lower()
                    if choice == "y":
                        if not current_reply:
                            log("  ⚠️  回复内容为空，不能发送")
                            break
                        log("  📤 正在发送...")
                        success = await reply_to_comment(client, c["index"], current_reply)
                        if success:
                            log("  ✅ 发送成功！")
                            sent_count += 1
                            await asyncio.sleep(REPLY_INTERVAL)  # 风控间隔
                        else:
                            log("  ❌ 发送失败，请检查浏览器状态")
                        break
                    elif choice == "e":
                        new_text = input("  输入新回复: ").strip()
                        if new_text:
                            current_reply = new_text
                            log(f"  ✏️  已更新: {current_reply}")
                        continue
                    elif choice == "n":
                        log("  ⏭️  已跳过")
                        break
                    elif choice == "q":
                        log(f"\n👋 退出，本次共回复 {sent_count} 条")
                        return
                    else:
                        log("  无效输入，请输入 y/e/n/q")

                log("")

            log(f"🎉 完成！本次共回复 {sent_count} 条评论")
        finally:
            await client.close()

    asyncio.run(run())


def cmd_list_videos(args) -> None:
    """查看账号已发布的视频列表"""
    max_count = getattr(args, "max", 20)
    json_out = getattr(args, "json", False)

    async def run():
        client = await get_client()
        try:
            videos = await get_my_videos(client, max_count)

            if json_out:
                print(json.dumps(videos, ensure_ascii=False, indent=2))
                return

            log(f"\n📹 共找到 {len(videos)} 个视频：\n")
            for v in videos[:max_count]:
                log(f"[{v['index']+1}] {v['title'][:50]}")
                log(f"     {v['url']}")
                log("")
        finally:
            await client.close()

    asyncio.run(run())


# ============================================================
# 辅助函数
# ============================================================
def extract_video_id(url: str) -> str:
    """从 URL 提取视频 ID"""
    m = re.search(r"video/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"v\.douyin\.com/([a-zA-Z0-9]+)", url)
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]", "_", url[:40])


def save_comments(video_id: str, comments: List[Dict]) -> None:
    """保存评论到本地"""
    data_dir = Path(os.environ.get("DOUYIN_DATA_DIR", str(USER_DATA_DIR)))
    data_dir.mkdir(parents=True, exist_ok=True)
    filepath = data_dir / f"comments_{video_id}.json"
    data = {"video_id": video_id, "total": len(comments), "comments": comments}
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"💾 已保存: {filepath}")


# ============================================================
# 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="抖音评论读取与回复工具（Mclaw 技能版）")
    sub = parser.add_subparsers(dest="command", help="命令")

    p = sub.add_parser("launch-browser", help="启动浏览器（Edge + CDP，扫码登录）")
    p.add_argument("--kill", action="store_true", help="关闭浏览器进程")

    sub.add_parser("status", help="检查浏览器连接状态")

    p = sub.add_parser("read", help="读取视频评论")
    p.add_argument("url", help="视频URL")
    p.add_argument("--max", type=int, default=30, help="最多读取条数")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("generate", help="生成回复建议（不发送）")
    p.add_argument("url", help="视频URL")
    p.add_argument("--max", type=int, default=20, help="最多处理条数")
    p.add_argument("--topic", default="", help="视频主题（帮助生成回复）")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("reply", help="逐条回复（人工确认）")
    p.add_argument("url", help="视频URL")
    p.add_argument("--max", type=int, default=10, help="最多回复条数")
    p.add_argument("--topic", default="", help="视频主题")

    p = sub.add_parser("list-videos", help="查看账号已发布的视频")
    p.add_argument("--max", type=int, default=20, help="最多显示条数")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "launch-browser": cmd_launch_browser,
        "status": cmd_status,
        "read": cmd_read,
        "generate": cmd_generate,
        "reply": cmd_reply,
        "list-videos": cmd_list_videos,
    }

    func = cmd_map.get(args.command)
    if func:
        func(args)


if __name__ == "__main__":
    main()
