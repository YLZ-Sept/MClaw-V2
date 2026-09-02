"""硬件指纹采集与容错匹配。

指纹形如 ``2F72-4463-9B50-7666-617F``：五个部件**各自独立**哈希，每个产出
一段。这是与「拼接后整体哈希」的关键区别——后者因哈希雪崩效应，任何单个
部件变化都会导致全部段位改变，所谓「5 选 3 容错」根本无法成立。独立哈希
后，换一块硬盘只废掉对应的那一段，其余四段仍然匹配。

取不到的部件记为 ``XXXX``，且**永不参与匹配**：否则两台都缺盘序列号的机器
会在该段上互相「匹配」，凭空削弱绑定强度。

采集实测约 1.25 秒（单次 PowerShell 调用），因此进程内缓存，绝不可在每个
请求上执行。硬件不会热变，缓存一次即可。
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

# 段位标识与显示名。顺序即指纹中的段序，**不可调整**——调整会使所有已签发
# 的授权码指纹失配。
FINGERPRINT_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("board", "主板序列号"),
    ("bios", "BIOS 序列号"),
    ("disk", "系统盘序列号"),
    ("mac", "物理网卡 MAC"),
    ("guid", "MachineGuid"),
)

# 部件取不到时的占位。匹配时跳过。
#
# 不能用 "----"：段分隔符也是 "-"，split("-") 会把它拆成空串。四位十六进制
# 段只含 0-9A-F，因此 "XXXX" 永不与真实段位碰撞。
MISSING_SEGMENT = "XXXX"

# 段位哈希盐。变更会使所有已签发授权码失效。
_SALT = b"mclaw-license-fp-v1"

# 判定为同一台机器所需的最少匹配段数（不含缺失段）。
MATCH_THRESHOLD = 3

# 激活时要求的最少可用段数。低于此值说明该机器几乎无法唯一标识，签发的
# 授权码必然在后续校验中失配——与其留个定时炸弹，不如当场拒绝。
MIN_USABLE_SEGMENTS = 3

_PS_TIMEOUT_SEC = 30

# 单次取回全部五项，避免五次进程启动的开销。每行对应一个部件，顺序与
# FINGERPRINT_SEGMENTS 一致；取不到的项输出空行。
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
function Emit($v) { if ($null -eq $v) { '' } else { "$v".Trim() } }
Emit (Get-CimInstance Win32_BaseBoard).SerialNumber
Emit (Get-CimInstance Win32_BIOS).SerialNumber
Emit (Get-CimInstance Win32_DiskDrive | Where-Object { $_.Index -eq 0 } |
      Select-Object -First 1).SerialNumber
Emit (Get-CimInstance Win32_NetworkAdapter |
      Where-Object { $_.PhysicalAdapter -eq $true -and $_.MACAddress } |
      Sort-Object DeviceID | Select-Object -First 1).MACAddress
Emit (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
"""

# 厂商在取不到真实值时填充的占位串。这些值在同型号机器间完全相同，
# 当作有效指纹会造成跨机器误匹配，必须视为缺失。
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "0",
        "00000000",
        "none",
        "null",
        "default string",
        "to be filled by o.e.m.",
        "to be filled by oem",
        "system serial number",
        "serial number",
        "not applicable",
        "not specified",
        "not available",
        "unknown",
        "invalid",
        "filled by oem",
        "x.x.x.x",
        "123456789",
        "0123456789",
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    }
)

_cache_lock = threading.Lock()
_cached_fingerprint: str | None = None
_cached_detail: dict[str, bool] | None = None


def _is_placeholder(value: str) -> bool:
    """厂商占位串检测。"""
    normalized = value.strip().lower()
    if normalized in _PLACEHOLDER_VALUES:
        return True
    # 全 0 / 全 F / 全同一字符（去掉分隔符后）通常是未烧录的占位。
    stripped = re.sub(r"[\s\-_.:]", "", normalized)
    if len(stripped) >= 4 and len(set(stripped)) == 1:
        return True
    return False


def _hash_segment(kind: str, value: str) -> str:
    """单个部件 → 4 位十六进制段。

    每个部件独立加盐哈希，并把 ``kind`` 混入摘要——否则两个不同部件恰好
    取到相同字符串时会产生相同段位。
    """
    payload = _SALT + kind.encode("ascii") + b":" + value.strip().lower().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:4].upper()


def _collect_raw_windows() -> list[str]:
    """执行 PowerShell 采集五项原始值。失败时返回全空。"""
    empty = ["" for _ in FINGERPRINT_SEGMENTS]
    if sys.platform != "win32":
        logger.warning("硬件指纹目前仅支持 Windows，当前平台 %s", sys.platform)
        return empty

    # 桌面端以 GUI 进程启动后端，不加 CREATE_NO_WINDOW 会闪黑框。
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _PS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SEC,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("硬件指纹采集失败: %s", exc)
        return empty

    if proc.returncode != 0:
        logger.error("硬件指纹采集返回 %s: %s", proc.returncode, proc.stderr[:200])

    lines = proc.stdout.splitlines()
    # 行数不足时补空——单项缺失不应拖垮其余段位。
    while len(lines) < len(FINGERPRINT_SEGMENTS):
        lines.append("")
    return [ln.strip() for ln in lines[: len(FINGERPRINT_SEGMENTS)]]


def compute_fingerprint() -> tuple[str, dict[str, bool]]:
    """采集并计算指纹，不走缓存。

    Returns:
        ``(fingerprint, detail)``；``detail`` 为 ``{段位标识: 是否可用}``。
    """
    raw_values = _collect_raw_windows()
    segments: list[str] = []
    detail: dict[str, bool] = {}

    for (kind, label), raw in zip(FINGERPRINT_SEGMENTS, raw_values, strict=True):
        if not raw or _is_placeholder(raw):
            segments.append(MISSING_SEGMENT)
            detail[kind] = False
            logger.debug("指纹段 %s (%s) 不可用", kind, label)
        else:
            segments.append(_hash_segment(kind, raw))
            detail[kind] = True

    return "-".join(segments), detail


def collect_fingerprint(*, refresh: bool = False) -> str:
    """返回本机指纹，进程内缓存。

    Args:
        refresh: 强制重新采集（仅供测试/诊断使用）。
    """
    global _cached_fingerprint, _cached_detail

    with _cache_lock:
        if _cached_fingerprint is not None and not refresh:
            return _cached_fingerprint
        fingerprint, detail = compute_fingerprint()
        _cached_fingerprint = fingerprint
        _cached_detail = detail
        logger.info(
            "硬件指纹: %s (可用段 %d/%d)",
            fingerprint,
            sum(detail.values()),
            len(FINGERPRINT_SEGMENTS),
        )
        return fingerprint


def fingerprint_detail(*, refresh: bool = False) -> dict[str, bool]:
    """返回各段位可用性，供激活页提示用户。"""
    collect_fingerprint(refresh=refresh)
    with _cache_lock:
        return dict(_cached_detail or {})


def usable_segment_count(fingerprint: str) -> int:
    """指纹中非缺失段的数量。"""
    return sum(1 for seg in fingerprint.split("-") if seg != MISSING_SEGMENT)


def fingerprint_matches(licensed: str, current: str) -> tuple[bool, int]:
    """比对授权码内的指纹与本机实时指纹。

    缺失段（``----``）在任一侧出现时都跳过，不计入匹配数——两台机器同样
    缺失某部件不构成「相同」的证据。

    Returns:
        ``(是否判定为同一台机器, 匹配段数)``
    """
    licensed_parts = licensed.strip().upper().split("-")
    current_parts = current.strip().upper().split("-")

    if len(licensed_parts) != len(FINGERPRINT_SEGMENTS):
        return False, 0
    if len(current_parts) != len(FINGERPRINT_SEGMENTS):
        return False, 0

    matched = 0
    for lic, cur in zip(licensed_parts, current_parts, strict=True):
        if lic == MISSING_SEGMENT or cur == MISSING_SEGMENT:
            continue
        if lic == cur:
            matched += 1

    return matched >= MATCH_THRESHOLD, matched


def reset_cache() -> None:
    """清空缓存（测试用）。"""
    global _cached_fingerprint, _cached_detail
    with _cache_lock:
        _cached_fingerprint = None
        _cached_detail = None
