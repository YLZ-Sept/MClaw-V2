#!/usr/bin/env python3
"""Verify that every skill declared to ship actually makes it into the wheel.

背景（为什么需要这个脚本）
--------------------------
2026-10-15 客户机事故：安装包装完，"技能"页比开发机少一大截。

根因不是"技能没登记"，恰恰相反——`skills/catalog.json`（技能市场分类）与
`src/mclaw/skills/loader.py` 的 `DEFAULT_DISABLED_SKILLS`（随包发布、默认
关闭）里**都登记了**这些技能，但真正决定 wheel 内容的
`[tool.hatch.build.targets.wheel.force-include]` 是一份**手工维护的白名单**，
长期没跟着仓库增长更新。结果：登记了 79 个，wheel 里只有 24 个 —— 客户机上
被登记过的技能一个也加载不到。

这类"清单漂移"和 wheelhouse 那次（见 `build/wheelhouse_check.py`）是同一个
病：真正的产物由一处清单决定，而"应该有什么"写在另一处，两者没人对账。

它做什么
--------
纯离线（不读网络、不必先构建）：

  1. DECLARED-BUT-UNPACKAGED —— catalog / DEFAULT_DISABLED_SKILLS 里声明
     要随包发布，但 force-include 白名单里没有。**这是本次事故的形态。**
  2. DANGLING-CATALOG       —— catalog 登记了，但仓库里没有对应技能目录。
  3. BROKEN-SOURCE          —— 白名单引用的 `skills/...` 源路径在磁盘上不存在
     （例如技能被改名/移动而白名单没跟），打包时才炸。
  4. ORPHAN-PACKAGED        —— 白名单里有，但两个清单都没登记（不致命，
     提醒补登记，否则用户无从在技能市场里看到它）。
  5. WHEEL-MISSING          —— 传了 `--wheel` 时：白名单里有、但该 wheel 实际
     不含这个技能。用于抓"白名单改了却没重建 wheel"。

1-3、5 任一命中即非零退出，可直接挂进打包流程做 gate。

用法
----
    python build/skills_package_check.py
    python build/skills_package_check.py --wheel <mclaw-*.whl>   # 加验 wheel 内容
    python build/skills_package_check.py --quiet                # 只在有问题时输出

用工具链 Python 跑（需要 `tomllib`，即 3.11+）。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CATALOG = ROOT / "skills" / "catalog.json"
LOADER = ROOT / "src" / "mclaw" / "skills" / "loader.py"
SKILLS_DIR = ROOT / "skills"

# force-include 里外部技能的目标前缀；只有落在它下面的才算"随包发布的外部技能"。
EXTERNAL_PREFIX = "mclaw/builtin_skills/external/"
# Python 侧读取随包技能时用的目录名（loader._builtin_skills_root()）。
WHEEL_EXTERNAL_DIR = "mclaw/builtin_skills/external/"

ERROR_KINDS = {
    "DECLARED-BUT-UNPACKAGED",
    "DANGLING-CATALOG",
    "BROKEN-SOURCE",
    "WHEEL-MISSING",
}


def configure_utf8_stdio() -> None:
    """让含中文的诊断输出在 Windows/git-bash 下不变成乱码（与
    `build/prepare_bootstrap_resources.py` 同款处理）。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except OSError:
                pass


def load_force_include() -> dict[str, str]:
    """pyproject 的 force-include 映射：源路径 -> wheel 内目标路径。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def load_catalog_skills() -> list[str]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    out: list[str] = []
    for category in data.get("categories", []):
        out.extend(category.get("skills", []))
    return out


def load_default_disabled() -> list[str]:
    """从 loader.py 里取 DEFAULT_DISABLED_SKILLS 的字面量集合。

    用 AST 而不是正则：这个集合带大量注释和分组，正则容易把注释里的词也吃进来。
    传 `frozenset({...})` 或 `frozenset([...])` 都能取到。
    """
    tree = ast.parse(LOADER.read_text(encoding="utf-8"))
    for node in tree.body:
        # 源码里是带注解的赋值（AnnAssign）；也容忍普通 Assign。
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        )
        if not any(getattr(t, "id", None) == "DEFAULT_DISABLED_SKILLS" for t in targets):
            continue
        return [
            n.value
            for n in ast.walk(node.value)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
    raise SystemExit(f"未能在 {LOADER} 中找到 DEFAULT_DISABLED_SKILLS")


def scan_repo_skills() -> dict[str, Path]:
    """仓库里实际存在的技能：目录名 -> 相对仓库根的目录路径。"""
    found: dict[str, Path] = {}
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        found[skill_md.parent.name] = skill_md.parent.relative_to(ROOT)
    return found


def packaged_external(force_include: dict[str, str]) -> dict[str, str]:
    """白名单里的外部技能：技能名 -> 源路径。"""
    out: dict[str, str] = {}
    for src, dst in force_include.items():
        if dst.startswith(EXTERNAL_PREFIX):
            out[dst[len(EXTERNAL_PREFIX):].strip("/")] = src
    return out


def wheel_contains(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return {
            name[len(WHEEL_EXTERNAL_DIR):].rsplit("/SKILL.md", 1)[0]
            for name in zf.namelist()
            if name.startswith(WHEEL_EXTERNAL_DIR) and name.endswith("/SKILL.md")
        }


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="额外校验该 wheel 是否真的含有白名单里的技能（抓『改了白名单没重建 wheel』）",
    )
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = parser.parse_args()

    force_include = load_force_include()
    catalog = load_catalog_skills()
    default_disabled = load_default_disabled()
    repo_skills = scan_repo_skills()
    packaged = packaged_external(force_include)

    declared = sorted(set(catalog) | set(default_disabled))
    problems: list[tuple[str, str, str]] = []  # (kind, name, detail)

    # 1. 声明要发布、却没进白名单 —— 本次事故的形态
    for name in declared:
        if name not in packaged:
            where = "catalog.json" if name in catalog else "DEFAULT_DISABLED_SKILLS"
            problems.append(("DECLARED-BUT-UNPACKAGED", name, f"登记在 {where}"))

    # 2. catalog 登记了、仓库里却没有目录
    for name in catalog:
        if name not in repo_skills:
            problems.append(("DANGLING-CATALOG", name, "技能市场登记了，但仓库里没有该技能目录"))

    # 3. 白名单源路径不存在（打包时才炸）
    for name, src in sorted(packaged.items()):
        if not (ROOT / src / "SKILL.md").is_file():
            problems.append(("BROKEN-SOURCE", name, f"白名单源路径不存在: {src}/SKILL.md"))

    # 4. 白名单里有、两个清单都没登记
    for name in sorted(packaged):
        if name not in catalog and name not in default_disabled:
            problems.append(("ORPHAN-PACKAGED", name, "已随包发布，但 catalog 与默认关闭表都没登记"))

    # 5. wheel 内容校验
    if args.wheel is not None:
        if not args.wheel.is_file():
            problems.append(("WHEEL-MISSING", str(args.wheel), "指定的 wheel 不存在"))
        else:
            in_wheel = wheel_contains(args.wheel)
            for name in sorted(packaged):
                if name not in in_wheel:
                    problems.append(
                        ("WHEEL-MISSING", name, f"白名单有，但 {args.wheel.name} 内没有")
                    )

    errors = [p for p in problems if p[0] in ERROR_KINDS]
    warnings = [p for p in problems if p[0] not in ERROR_KINDS]

    if not args.quiet or problems:
        print(f"仓库技能目录      : {len(repo_skills)}")
        print(f"catalog 登记      : {len(catalog)}")
        print(f"默认关闭表        : {len(default_disabled)}")
        print(f"声明应发布(并集)  : {len(declared)}")
        print(f"白名单外部技能    : {len(packaged)}")
        print()

    for kind, name, detail in warnings:
        print(f"[WARN] {kind:<17} {name}  ({detail})")
    for kind, name, detail in errors:
        print(f"[ERR ] {kind:<17} {name}  ({detail})")

    if errors:
        print(f"\n{len(errors)} 个错误 —— 安装包里的技能会对不上，先修再打包。")
        return 1
    if not args.quiet:
        print("技能打包清单一致 ✓" if not warnings else "技能打包清单一致（有警告）✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
