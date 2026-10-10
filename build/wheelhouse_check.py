#!/usr/bin/env python3
"""Verify that the bundled offline wheelhouse actually satisfies the app's closure.

背景（为什么需要这个脚本）
--------------------------
`apps/setup-center/src-tauri/resources/bootstrap/wheelhouse/` 是随安装包分发的
离线依赖集。当 `manifest.json` 里 `wheelhouse.complete == true` 时，Tauri 运行时
会给 `uv pip install` 传 `--no-index --find-links <wheelhouse>`（见
`src-tauri/src/runtime.rs`）——**完全不回落到 pip 源**。

于是任何"漏进 wheelhouse 的依赖"都会在客户机首启时直接安装失败；更糟的是
*静默*类问题：依赖装上了但功能没了（例如 jieba 缺失 → 知识库中文检索
静默失效，见 2026-08 / 2026-10 两次事故）。

历史教训：`pyproject.toml` 加了新依赖后，wheelhouse **不会自动更新**，而
`mclaw-*.whl` 本身还是旧构建（METADATA 里根本没有那条 `Requires-Dist`），
所以连"把缺的 wheel 丢进目录"都不够——必须重建 mclaw wheel。本脚本把这条
链路变成一次可失败的检查。

它做什么
--------
纯离线（不访问网络）：读取 wheelhouse 内每个 wheel 的 `METADATA`，以
`mclaw` wheel 为根、按请求的 extra（默认 `desktop`）解析依赖闭包，逐条核对：

  1. MISSING   —— wheelhouse 里完全没有这个包
  2. PLATFORM  —— 有 wheel，但 tag 与目标平台/ABI 不匹配（装了也用不上）
  3. VERSION   —— 有 wheel，但没有满足 specifier 的版本

任何一条都让进程以非零码退出，可直接挂进打包流程做 gate。

用法
----
    python build/wheelhouse_check.py                 # 校验出厂 wheelhouse
    python build/wheelhouse_check.py --extra desktop --extra windows
    python build/wheelhouse_check.py --download-missing   # 顺带补齐（需要网络）
    python build/wheelhouse_check.py --mark-complete      # 校验通过才写 manifest

依赖 `packaging`（解析 specifier / marker）。用工具链 Python 跑，不要用
bootstrap 种子解释器——种子是精简版，没有 `packaging`。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from collections import deque
from pathlib import Path

try:
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - 环境缺失时给出可操作的提示
    sys.exit(
        "需要 `packaging`（解析 specifier / 环境 marker）。\n"
        "请用工具链 Python 运行，例如：.venv/Scripts/python.exe build/wheelhouse_check.py\n"
        "或 `pip install packaging`。"
    )


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


configure_utf8_stdio()

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_DIR = ROOT / "apps" / "setup-center" / "src-tauri" / "resources" / "bootstrap"
DEFAULT_WHEELHOUSE = BOOTSTRAP_DIR / "wheelhouse"
MANIFEST_PATH = BOOTSTRAP_DIR / "manifest.json"

# 目标平台 → (wheel 文件名里的 platform tag, PEP 508 环境 marker 变量)
# 目前出厂 wheelhouse 只有 win-x64 一份（cp311 + py3-none-any）。
PLATFORMS: dict[str, dict[str, str]] = {
    "win-amd64": {
        "tag": "win_amd64",
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "os_name": "nt",
    },
    "linux-x86_64": {
        "tag": "manylinux_2_17_x86_64",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "x86_64",
        "os_name": "posix",
    },
    "macos-arm64": {
        "tag": "macosx_11_0_arm64",
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "os_name": "posix",
    },
}

# wheel 文件名: {name}-{version}(-{build})?-{pytag}-{abitag}-{platformtag}.whl
WHEEL_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<pytag>[^-]+)-(?P<abitag>[^-]+)-(?P<platformtag>[^-]+)\.whl$"
)


class WheelInfo:
    """一个 wheel 文件名 + 其 METADATA 里的 Requires-Dist。"""

    __slots__ = ("path", "name", "version", "pytags", "abitags", "platformtags", "requires")

    def __init__(
        self,
        path: Path,
        name: str,
        version: str,
        pytags: list[str],
        abitags: list[str],
        platformtags: list[str],
        requires: list[str],
    ) -> None:
        self.path = path
        self.name = name
        self.version = version
        self.pytags = pytags
        self.abitags = abitags
        self.platformtags = platformtags
        self.requires = requires


def parse_wheel_filename(path: Path) -> tuple[str, str, list[str], list[str], list[str]] | None:
    m = WHEEL_RE.match(path.name)
    if not m:
        return None
    return (
        canonicalize_name(m.group("name")),
        m.group("version"),
        m.group("pytag").split("."),
        m.group("abitag").split("."),
        m.group("platformtag").split("."),
    )


def read_requires_dist(path: Path) -> list[str]:
    """从 wheel 里读 `.dist-info/METADATA` 的 Requires-Dist 原始行。"""
    try:
        with zipfile.ZipFile(path) as zf:
            meta_names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if not meta_names:
                return []
            text = zf.read(meta_names[0]).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[WARN] 无法读取 {path.name}: {exc}", file=sys.stderr)
        return []

    out: list[str] = []
    for line in text.splitlines():
        # RFC822 续行（以空格/Tab 开头）理论上存在于长字段；Requires-Dist 不常见但兜一下。
        if line.startswith("Requires-Dist:"):
            out.append(line[len("Requires-Dist:") :].strip())
    return out


def _cp_version(pytag: str) -> tuple[int, int] | None:
    """ "cp311" -> (3, 11)；非 cp 前缀返回 None。"""
    if not pytag.startswith("cp"):
        return None
    digits = pytag[2:]
    if not digits.isdigit():
        return None
    return (int(digits[0]), int(digits[1:] or "0"))


def _py_abi_compatible(pytag: str, abitag: str, mm: str) -> bool:
    """`mm` 形如 "311"。

    关键：`abi3` 是 CPython **稳定 ABI**，标记为 `cpXY-abi3` 的 wheel 对其后所有
    版本都可用（例如 psutil 只有 `cp37-abi3`、primp 只有 `cp310-abi3`，在 3.11 上
    完全合法）。按"版本元组 ≤ 目标"判定，不能只做字符串等值。
    """
    if pytag in {"py3", "py2.py3", "py3.py2"}:
        return True
    if pytag == f"py{mm}":
        return True

    cp_ver = _cp_version(pytag)
    if cp_ver is None:
        return False

    target = (int(mm[0]), int(mm[1:]))
    if abitag == "abi3":
        return cp_ver <= target
    return cp_ver == target


def _platform_tag_compatible(ptag: str, target_tag: str) -> bool:
    if ptag == "any":
        return True
    if ptag == target_tag:
        return True
    # manylinux_2_17_x86_64 / manylinux2014_x86_64 同族：架构后缀一致即可。
    if ptag.startswith("manylinux") and target_tag.startswith("manylinux"):
        return ptag.split("_")[-2:] == target_tag.split("_")[-2:]
    return False


def wheel_is_compatible(info: WheelInfo, python_mm: str, target_tag: str) -> bool:
    py_ok = any(_py_abi_compatible(pt, ab, python_mm) for pt in info.pytags for ab in info.abitags)
    if not py_ok:
        return False
    return any(_platform_tag_compatible(t, target_tag) for t in info.platformtags)


def load_wheelhouse(
    wheelhouse: Path, python_mm: str, target_tag: str
) -> tuple[dict[str, dict[str, WheelInfo]], dict[str, dict[str, WheelInfo]]]:
    """返回 (可用, 平台不匹配)。键为规范化包名，值为 {version: WheelInfo}。"""
    available: dict[str, dict[str, WheelInfo]] = {}
    incompatible: dict[str, dict[str, WheelInfo]] = {}

    for path in sorted(wheelhouse.glob("*.whl")):
        parsed = parse_wheel_filename(path)
        if parsed is None:
            print(f"[WARN] 文件名无法解析，已跳过: {path.name}", file=sys.stderr)
            continue
        name, version, pytags, abitags, platformtags = parsed
        info = WheelInfo(
            path, name, version, pytags, abitags, platformtags, read_requires_dist(path)
        )
        bucket = available if wheel_is_compatible(info, python_mm, target_tag) else incompatible
        bucket.setdefault(name, {})[version] = info

    return available, incompatible


def build_env(platform: dict[str, str], python_mm: str, extra: str) -> dict[str, str]:
    """构造 PEP 508 marker 求值环境。`extra` 总是显式给出（空串代表"不带 extra"），
    否则含 `extra == ...` 的 marker 会因变量缺失而抛 UndefinedEnvironmentName。"""
    full = f"{python_mm[0]}.{python_mm[1:]}.0"
    return {
        "implementation_name": "cpython",
        "implementation_version": full,
        "os_name": platform["os_name"],
        "platform_machine": platform["platform_machine"],
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": platform["platform_system"],
        "python_full_version": full,
        "python_version": f"{python_mm[0]}.{python_mm[1:]}",
        "sys_platform": platform["sys_platform"],
        "extra": extra,
    }


def marker_applies(
    marker: Marker | str | None, platform: dict[str, str], python_mm: str, extras: set[str]
) -> bool:
    """判断一条 Requires-Dist 是否适用于 `extras` 中的任一 extra。

    注意 `Requirement.marker` 已经是 `Marker` **对象**，不能再拿去 `Marker(...)`
    构造一次（pyparsing 收到非字符串会抛，被兜底成"适用"→ 满屏误报）。
    这里对 str / Marker / None 都做兼容。

    对候选 extra 逐个求值取"或"即可，不需要探测 marker 是否引用 `extra`：
    marker 不含 `extra` 变量时（基础依赖），各次求值结果天然一致。
    """
    if marker is None:
        return True
    if isinstance(marker, str):
        try:
            marker = Marker(marker)
        except Exception:  # marker 语法异常时不静默丢弃，按"适用"处理以暴露问题
            print(f"[WARN] 无法解析 marker: {marker!r}", file=sys.stderr)
            return True

    for extra in extras or {""}:
        try:
            if marker.evaluate(build_env(platform, python_mm, extra)):
                return True
        except Exception as exc:
            print(f"[WARN] marker 求值失败 {marker}: {exc}", file=sys.stderr)
            return True
    return False


class Problem:
    __slots__ = ("kind", "message")

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message


def resolve(
    root_wheel: WheelInfo,
    extras: set[str],
    available: dict[str, dict[str, WheelInfo]],
    incompatible: dict[str, dict[str, WheelInfo]],
    platform: dict[str, str],
    python_mm: str,
) -> tuple[dict[str, str], list[Problem], set[str]]:
    """从 root_wheel 出发解析闭包。

    返回 (已解析 name->version, 问题列表, 缺失需求集合——供 --download-missing 用)。
    """
    resolved: dict[str, str] = {}
    problems: list[Problem] = []
    missing_reqs: set[str] = set()
    seen: set[tuple[str, frozenset[str]]] = set()
    queue: deque[tuple[WheelInfo, frozenset[str]]] = deque([(root_wheel, frozenset(extras))])

    while queue:
        info, req_extras = queue.popleft()
        key = (info.name, req_extras)
        if key in seen:
            continue
        seen.add(key)
        resolved[info.name] = info.version

        for raw in info.requires:
            try:
                req = Requirement(raw)
            except Exception:
                print(f"[WARN] 无法解析依赖声明 {raw!r}（来自 {info.name}）", file=sys.stderr)
                continue

            if not marker_applies(req.marker, platform, python_mm, set(req_extras)):
                continue

            dep = canonicalize_name(req.name)
            candidates = available.get(dep, {})
            satisfying = _pick_satisfying(candidates, req.specifier)

            if satisfying is not None:
                queue.append((satisfying, frozenset(canonicalize_name(e) for e in req.extras)))
                continue

            # 未满足 —— 区分三种成因，消息要能直接指导修复
            label = f"{req.name}{req.specifier}"
            if req.extras:
                label += f"[{','.join(sorted(req.extras))}]"
            origin = info.name
            if req_extras:
                origin += f"[{','.join(sorted(req_extras))}]"

            if candidates:
                have = ", ".join(sorted(candidates))
                problems.append(
                    Problem(
                        "VERSION",
                        f"{label}  ← {origin}：wheelhouse 有 {dep} 但版本为 {have}，不满足",
                    )
                )
            elif incompatible.get(dep):
                have = ", ".join(sorted(incompatible[dep]))
                problems.append(
                    Problem(
                        "PLATFORM",
                        f"{label}  ← {origin}：wheelhouse 有 {dep}（{have}），但 tag 与目标 "
                        f"{platform['tag']}/cp{python_mm} 不匹配",
                    )
                )
            else:
                problems.append(Problem("MISSING", f"{label}  ← {origin}：wheelhouse 内没有 {dep}"))
                missing_reqs.add(raw)

    return resolved, problems, missing_reqs


def _pick_satisfying(candidates: dict[str, WheelInfo], specifier: SpecifierSet) -> WheelInfo | None:
    """在候选版本里挑一个满足 specifier 的（取最高版本）。"""
    best: tuple[Version, WheelInfo] | None = None
    for version_str, info in candidates.items():
        try:
            version = Version(version_str)
        except InvalidVersion:
            continue
        try:
            if specifier and version not in specifier:
                continue
        except InvalidSpecifier:
            continue
        if best is None or version > best[0]:
            best = (version, info)
    return best[1] if best else None


def download_missing(
    requirements: set[str],
    wheelhouse: Path,
    index_url: str,
    trusted_host: str,
    platform: dict[str, str],
    python_mm: str,
) -> int:
    """用 pip 把缺失依赖补齐进 wheelhouse（需要网络）。

    sdist 一律转成 wheel：`uv pip install --no-index` 不会现场构建源码包。
    """
    if not requirements:
        return 0

    print(f"[fill] 补齐 {len(requirements)} 个缺失依赖 ...")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        *sorted(requirements),
        "--dest",
        str(wheelhouse),
        "--only-binary=:all:",
        "--python-version",
        f"{python_mm[0]}.{python_mm[1:]}",
        "--platform",
        platform["tag"],
        "--implementation",
        "cp",
    ]
    if index_url:
        cmd += ["--index-url", index_url]
    if trusted_host:
        cmd += ["--trusted-host", trusted_host]

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(
            "[fill] pip download 失败。若失败原因是镜像上只有 sdist（常见：jieba / PyAutoGUI 系），\n"
            "       需要改走 `pip wheel <pkg>` 转成 wheel 再放入 wheelhouse。",
            file=sys.stderr,
        )
    return result.returncode


def load_pyproject() -> dict:
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            sys.exit("需要 tomllib（Python 3.11+）或 tomli 来读取 pyproject.toml")
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _req_key(req: Requirement) -> tuple[str, str, tuple[str, ...]]:
    return (canonicalize_name(req.name), str(req.specifier), tuple(sorted(req.extras)))


def _split_requires(wheel: WheelInfo, platform: dict[str, str], python_mm: str, extras: set[str]):
    """把 wheel 的 Requires-Dist 拆成 (基础依赖, {extra: 依赖})。

    METADATA 里"基础依赖"不带 marker（或 marker 不涉及 `extra`）；带
    `extra == "..."` 的归到对应 extra 下。
    """
    base: dict[tuple[str, str, tuple[str, ...]], Requirement] = {}
    per_extra: dict[str, dict[tuple[str, str, tuple[str, ...]], Requirement]] = {
        e: {} for e in extras
    }

    for raw in wheel.requires:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        marked_for_extra = req.marker is not None and "extra" in str(req.marker)
        if not marked_for_extra:
            if marker_applies(req.marker, platform, python_mm, set()):
                base[_req_key(req)] = req
            continue
        for extra in extras:
            if marker_applies(req.marker, platform, python_mm, {extra}):
                per_extra[extra][_req_key(req)] = req
    return base, per_extra


def check_wheel_freshness(
    root_wheel: WheelInfo, extras: set[str], platform: dict[str, str], python_mm: str
) -> list[Problem]:
    """对照 pyproject.toml，抓"mclaw wheel 没重建"。

    这是本脚本存在的另一半理由：只核对 wheelhouse 目录里的文件是不够的——
    如果 mclaw wheel 自身是旧构建，它的 METADATA 里根本没有新依赖，那么
    "缺 wheel" 这件事**根本不会进入依赖图**，校验会假通过。

    （2026-10-10 实况：pyproject 已加 `jieba>=0.42.1`，但 wheelhouse 里的
    mclaw wheel 仍是加之前的构建，光把 jieba.whl 丢进目录毫无作用。）
    """
    project = load_pyproject().get("project", {})
    optional = project.get("optional-dependencies", {}) or {}

    actual_base, actual_extra = _split_requires(root_wheel, platform, python_mm, extras)

    def to_map(decls: list[str]) -> dict[tuple[str, str, tuple[str, ...]], Requirement]:
        out: dict[tuple[str, str, tuple[str, ...]], Requirement] = {}
        for decl in decls:
            try:
                req = Requirement(decl)
            except Exception:
                continue
            out[_req_key(req)] = req
        return out

    problems: list[Problem] = []

    def diff(label: str, expected: dict, actual: dict) -> None:
        for key, req in sorted(expected.items()):
            if key not in actual:
                problems.append(
                    Problem(
                        "STALE-WHEEL", f"pyproject 声明了 {req} 但 mclaw wheel 里没有（{label}）"
                    )
                )
        for key, req in sorted(actual.items()):
            if key not in expected:
                problems.append(
                    Problem(
                        "STALE-WHEEL", f"mclaw wheel 里有 {req} 但 pyproject 已不声明（{label}）"
                    )
                )

    diff("基础依赖", to_map(project.get("dependencies", []) or []), actual_base)
    for extra in sorted(extras):
        diff(f"extra={extra}", to_map(optional.get(extra, []) or []), actual_extra.get(extra, {}))

    return problems


def set_manifest_complete(manifest_path: Path, complete: bool) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wheelhouse = manifest.setdefault("wheelhouse", {})
    wheelhouse["complete"] = complete
    wheelhouse.setdefault("path", "wheelhouse/")
    wheelhouse.setdefault(
        "note",
        "Bundled dependency wheels for offline install; includes locally-built mclaw-plugin-sdk.",
    )
    # newline="\n" 必须显式给：Windows 上 write_text 默认把 \n 翻成 \r\n，
    # 会让整个 manifest 变成一份无意义的全文 diff（prepare_bootstrap_resources.py
    # 的 write_manifest 也是这么处理的）。
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[manifest] wheelhouse.complete = {str(complete).lower()}  ({manifest_path})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--wheelhouse", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument(
        "--extra",
        action="append",
        default=None,
        help="要解析的 extra（可重复；默认 desktop —— 即 Tauri Setup Center 首装的目标）",
    )
    parser.add_argument(
        "--python-version",
        default="3.11",
        help="目标 Python major.minor（默认 3.11，与 PBS 种子对齐）",
    )
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="win-amd64")
    parser.add_argument(
        "--download-missing", action="store_true", help="把缺失依赖下载进 wheelhouse（需要网络）"
    )
    parser.add_argument(
        "--index-url", default="", help="下载用的 pip 源（默认取 manifest.default_pip_index）"
    )
    parser.add_argument("--trusted-host", default="")
    parser.add_argument(
        "--mark-complete",
        action="store_true",
        help="校验通过后写 manifest.json 的 wheelhouse.complete=true；不通过则保持不动并退出非零",
    )
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = parser.parse_args()

    if not args.wheelhouse.is_dir():
        print(f"[FAIL] wheelhouse 目录不存在: {args.wheelhouse}", file=sys.stderr)
        return 2

    python_mm = args.python_version.replace(".", "")
    platform = PLATFORMS[args.platform]
    extras = {e.strip() for e in (args.extra or ["desktop"]) if e.strip()}

    available, incompatible = load_wheelhouse(args.wheelhouse, python_mm, platform["tag"])
    if not available:
        print(
            f"[FAIL] {args.wheelhouse} 内没有与 {args.platform}/{args.python_version} 兼容的 wheel",
            file=sys.stderr,
        )
        return 2

    root_candidates = available.get("mclaw", {})
    if not root_candidates:
        print("[FAIL] wheelhouse 内找不到 mclaw wheel（无法确定解析根）", file=sys.stderr)
        return 2
    root = _pick_satisfying(root_candidates, SpecifierSet(""))
    assert root is not None

    resolved, problems, missing_reqs = resolve(
        root, extras, available, incompatible, platform, python_mm
    )
    # 先补"wheel 是否比 pyproject 旧"——否则旧 wheel 会让上面的闭包解析假通过。
    problems = check_wheel_freshness(root, extras, platform, python_mm) + problems

    if not args.quiet:
        print(f"wheelhouse : {args.wheelhouse}")
        print(f"根         : {root.path.name}")
        print(f"目标       : {args.platform} / cp{python_mm} / extras={sorted(extras)}")
        print(
            f"候选 wheel : {sum(len(v) for v in available.values())} 可用"
            f"（另有 {sum(len(v) for v in incompatible.values())} 个平台不匹配）"
        )
        print(f"解析闭包   : {len(resolved)} 个包")

    if args.download_missing and missing_reqs:
        rc = download_missing(
            missing_reqs,
            args.wheelhouse,
            args.index_url or _manifest_index_url(),
            args.trusted_host or _manifest_trusted_host(),
            platform,
            python_mm,
        )
        if rc != 0:
            return rc
        print("[fill] 已补齐，请重跑本脚本确认（新 wheel 需要重新解析闭包）")
        return 0

    if problems:
        print(f"\n[FAIL] 发现 {len(problems)} 个问题：\n")
        for kind in ("MISSING", "STALE-WHEEL", "PLATFORM", "VERSION"):
            group = [p for p in problems if p.kind == kind]
            for p in group:
                print(f"  {kind:11} {p.message}")
        print(
            "\n修复指引：缺失包用 --download-missing 补齐；若镜像只有 sdist，"
            "需 `pip wheel` 转 whl；若 mclaw 自身新增了依赖，记得重建 mclaw wheel 并同步 manifest 的 wheel.sha256。"
        )
        if args.mark_complete:
            print("\n[manifest] 校验未通过，拒绝写 wheelhouse.complete=true")
        return 1

    print(f"\n[OK] 闭包完整：{len(resolved)} 个包全部可由 {args.wheelhouse} 离线满足")
    if args.mark_complete:
        set_manifest_complete(MANIFEST_PATH, True)
    return 0


def _manifest_index_url() -> str:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return (manifest.get("default_pip_index") or {}).get("url", "")
    except (OSError, json.JSONDecodeError):
        return ""


def _manifest_trusted_host() -> str:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return (manifest.get("default_pip_index") or {}).get("trusted_host", "")
    except (OSError, json.JSONDecodeError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
