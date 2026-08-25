#!/usr/bin/env python3
"""企业微信 wecom-cli 快速命令封装。"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def ensure_cli():
    if not shutil.which("wecom-cli"):
        print("错误: wecom-cli 未安装，请先运行 setup.py", file=sys.stderr)
        sys.exit(1)


def run_cli(args: list[str]):
    cmd = ["wecom-cli"] + args
    print(f"→ {' '.join(cmd)}")
    if os.name == "nt":
        # Windows: npm 全局命令是 .cmd shim，需经 shell 执行
        result = subprocess.run(subprocess.list2cmdline(cmd), shell=True)
    else:
        result = subprocess.run(cmd)
    sys.exit(result.returncode)


def cmd_send_msg(args):
    ensure_cli()
    run_cli([
        "message", "send",
        "--chat-id", args.to,
        "--msg-type", "text",
        "--text", json.dumps({"content": args.content}, ensure_ascii=False),
    ])


def cmd_contacts(args):
    ensure_cli()
    if args.keywords:
        run_cli(["contact", "users", "search", "--keywords", *args.keywords])
    else:
        run_cli(["contact", "users", "search", "--search-mode", "list"])


def cmd_create_doc(args):
    ensure_cli()
    run_cli(["doc", "create", "--doc-name", args.title])


def cmd_schedule(args):
    ensure_cli()
    run_cli(["calendar", "schedules", "list"])


def main():
    parser = argparse.ArgumentParser(description="企业微信 wecom-cli 快速命令")
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send-msg", help="发送文本消息")
    p_send.add_argument("--to", required=True, help="接收人 userid")
    p_send.add_argument("--content", required=True, help="消息内容")

    p_contacts = sub.add_parser("contacts", help="通讯录成员搜索")
    p_contacts.add_argument("--keywords", nargs="*", help="搜索关键词（可多个），不传则全量列表")

    p_doc = sub.add_parser("create-doc", help="创建文档（doc 类型）")
    p_doc.add_argument("--title", required=True, help="文档标题")

    sub.add_parser("schedule", help="日程列表")

    args = parser.parse_args()
    {
        "send-msg": cmd_send_msg,
        "contacts": cmd_contacts,
        "create-doc": cmd_create_doc,
        "schedule": cmd_schedule,
    }[args.command](args)


if __name__ == "__main__":
    main()