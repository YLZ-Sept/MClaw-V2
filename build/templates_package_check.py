#!/usr/bin/env python3
"""校验企业运营内置模板是否随 wheel 打包。

模板放在 ``src/mclaw/builtin_templates/enterprise_ops/``，位于包目录内，
hatchling 默认会随 package 一起打进 wheel。本脚本做一次显式对账，防止：

1. 源文件被移动/删除，打包时才炸；
2. 以后有人把模板挪出 ``src/mclaw``，却没补 force-include，客户机知识库里
   只有空骨架、没有模板。

用法::

    python build/templates_package_check.py
    python build/templates_package_check.py --wheel <mclaw-*.whl>
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "src" / "mclaw" / "builtin_templates" / "enterprise_ops"
WHEEL_PREFIX = "mclaw/builtin_templates/enterprise_ops/"
EXPECTED_COUNT = 10
ALLOWED_SUFFIXES = {".xlsx", ".docx"}


def configure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except OSError:
                pass


def source_files() -> list[Path]:
    return sorted(
        p
        for p in TEMPLATE_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="校验企业运营内置模板打包")
    parser.add_argument("--wheel", type=Path, default=None, help="可选：同时校验 wheel 内容")
    args = parser.parse_args()
    configure_utf8_stdio()

    files = source_files()
    errors: list[str] = []
    if len(files) != EXPECTED_COUNT:
        errors.append(f"源文件数量 {len(files)} != 期望 {EXPECTED_COUNT}")
    if not files:
        errors.append(f"模板目录为空: {TEMPLATE_DIR}")

    print(f"源模板: {len(files)} 份")
    for path in files:
        print(f"  - {path.relative_to(TEMPLATE_DIR)}")

    if args.wheel:
        wheel = args.wheel
        if not wheel.is_file():
            errors.append(f"wheel 不存在: {wheel}")
        else:
            with zipfile.ZipFile(wheel) as zf:
                names = set(zf.namelist())
            print(f"\n校验 wheel: {wheel.name}")
            for path in files:
                rel = path.relative_to(TEMPLATE_DIR).as_posix()
                expected = WHEEL_PREFIX + rel
                if expected not in names:
                    errors.append(f"wheel 缺少: {expected}")
                else:
                    print(f"  OK {rel}")

    if errors:
        print("\nFAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
