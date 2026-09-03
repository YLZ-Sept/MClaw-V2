#!/usr/bin/env python3
"""把 vendor/noble-ed25519.js 内联进 index.html，产出可双击运行的单文件。

为什么必须内联：``file://`` 下浏览器按 CORS 规则拒绝加载 ES module，
外链 <script type="module"> 会静默失败。商务要的是"双击就能用"，
所以把库塞进 <script> 标签里。

    python tools/license_web/build.py

改动 index.html 的模板部分或升级 vendor 库后重跑。
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK_BEGIN = "/* ==== VENDOR:noble-ed25519 BEGIN (由 build.py 注入，勿手改) ==== */"
MARK_END = "/* ==== VENDOR:noble-ed25519 END ==== */"


def main() -> int:
    page = HERE / "index.html"
    lib = HERE / "vendor" / "noble-ed25519.js"

    html = page.read_text(encoding="utf-8")
    code = lib.read_text(encoding="utf-8")

    if MARK_BEGIN not in html:
        raise SystemExit(
            f"index.html 里找不到注入标记，请确认模板包含:\n  {MARK_BEGIN}\n  {MARK_END}"
        )

    block = f"{MARK_BEGIN}\n{code}\n{MARK_END}"
    new = re.sub(
        re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END),
        lambda _: block,
        html,
        flags=re.S,
    )
    page.write_text(new, encoding="utf-8")

    size = len(new.encode("utf-8"))
    print(f"已注入 {lib.name} → {page.name}")
    print(f"  单文件大小 {size / 1024:.0f} KB（双击即可用，无需联网）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
