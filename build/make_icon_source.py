#!/usr/bin/env python
"""从 src/assets/logo-new.png 生成透明底的方形图标源图。

源图是 2048x2048 的**白底**方图（alpha 全为 255），直接喂给 `npx tauri icon`
会让 exe / 托盘 / 任务栏图标带一个白色方块。这里裁成 squircle 圆角方形、抠掉
外部白底，再加一道淡内描边 —— 白底图标在浅色任务栏上不描边几乎看不见边缘。

用法：
    python build/make_icon_source.py [源图] [输出]

生成后接着跑（顺序别反，logo.ico 要跟 icon.ico 同一份）：
    cd apps/setup-center && npx tauri icon ../../build/icon-source.png
    cp src-tauri/icons/icon.ico src-tauri/icons/logo.ico

注意：源图的圆角方形边界是 AI 生成的软过渡，没有硬边可对齐；下面这几个常数是
按视觉效果定的，改之前先看一眼预览。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent

SIZE = 1024
MARGIN_PCT = 4.0    # 留白：源图的圆角方形并不贴边
RADIUS_PCT = 22.0   # 圆角半径占图标边长比例（近似 iOS squircle）
FEATHER = 1.5       # 遮罩羽化，消掉源图的软边
BORDER_ALPHA = 70   # 内描边不透明度，0 = 不描边
BORDER_WIDTH = 4
BORDER_COLOR = (20, 32, 58)


def build(src: Path, out: Path, border_alpha: int = BORDER_ALPHA) -> Path:
    base = Image.open(src).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)

    margin = int(SIZE * MARGIN_PCT / 100)
    radius = int((SIZE - 2 * margin) * RADIUS_PCT / 100)
    box = (margin, margin, SIZE - margin, SIZE - margin)

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(FEATHER))

    icon = base.copy()
    icon.putalpha(mask)

    if border_alpha:
        ring = Image.new("L", (SIZE, SIZE), 0)
        ImageDraw.Draw(ring).rounded_rectangle(
            box, radius=radius, outline=border_alpha, width=BORDER_WIDTH
        )
        ring = ring.filter(ImageFilter.GaussianBlur(1.2))
        # 只保留落在图标内部的那一半描边，否则外圈会在深色底上发虚
        ring = Image.composite(ring, Image.new("L", (SIZE, SIZE), 0), mask)
        stroke = Image.new("RGBA", (SIZE, SIZE), BORDER_COLOR + (0,))
        stroke.putalpha(ring)
        icon = Image.alpha_composite(icon, stroke)

    out.parent.mkdir(parents=True, exist_ok=True)
    icon.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "src",
        nargs="?",
        default=_REPO_ROOT / "apps" / "setup-center" / "src" / "assets" / "logo-new.png",
        type=Path,
    )
    ap.add_argument("out", nargs="?", default=_THIS_DIR / "icon-source.png", type=Path)
    ap.add_argument(
        "--border-alpha",
        type=int,
        default=BORDER_ALPHA,
        help="内描边不透明度，0 关闭（默认 %(default)s）",
    )
    args = ap.parse_args()

    if not args.src.exists():
        print(f"[ERR] 源图不存在: {args.src}")
        return 1

    path = build(args.src, args.out, args.border_alpha)
    print(f"[OK] {args.src} -> {path}  ({SIZE}x{SIZE}, 透明底)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
