"""授权状态机、持久化与时钟回拨防护。

设计要点：

**每次进程启动重新完整校验。** 不信任 ``data/license.json`` 里的任何状态
标记——授权码原文每次重新验签、指纹每次重新采集比对。改数据库/JSON 里的
``status`` 字段无法绕过。

**指纹不在每个请求上采集。** 采集耗时约 1.25 秒，每请求执行会直接废掉
接口。启动时算一次，结果缓存在内存；中间件每请求只读缓存状态并比对日期，
均为纯内存操作。

**时钟回拨防护。** 记录单调递增的 ``last_seen_utc`` 水位线，每小时节流写
一次。若发现当前时间早于水位线超过容差，判定为篡改。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .fingerprint import (
    MIN_USABLE_SEGMENTS,
    collect_fingerprint,
    fingerprint_matches,
    usable_segment_count,
)
from .verifier import LicensePayload, LicenseVerifyError, verify_license_code

logger = logging.getLogger(__name__)

_LICENSE_FILE = "license.json"

# 到期前多少天开始在前端提示续费。
WARN_BEFORE_DAYS = 30

# 到期后的宽限天数：功能仍全开但持续告警，避免续费在途时把付费客户锁在门外。
GRACE_PERIOD_DAYS = 7

# 时钟水位线写盘节流间隔。
_HEARTBEAT_INTERVAL = timedelta(hours=1)

# 允许的时间倒退容差。覆盖时区调整、NTP 校正、虚拟机挂起恢复等正常情况。
_CLOCK_TOLERANCE = timedelta(hours=24)


class LicenseState(str, Enum):
    """授权状态。"""

    ACTIVE = "active"
    """有效。"""

    GRACE = "grace"
    """已过期但在宽限期内——功能不受限，前端持续告警。"""

    MISSING = "missing"
    """未激活。"""

    INVALID = "invalid"
    """验签失败、内容损坏或检测到时钟篡改。"""

    MISMATCH = "mismatch"
    """指纹不匹配——授权码来自另一台机器。"""

    EXPIRED = "expired"
    """已过期且宽限期用尽。"""

    @property
    def allows_access(self) -> bool:
        """该状态下是否放行业务接口。"""
        return self in (LicenseState.ACTIVE, LicenseState.GRACE)


@dataclass(frozen=True)
class LicenseStatus:
    """对外暴露的授权状态快照。"""

    state: LicenseState
    payload: LicensePayload | None = None
    message: str = ""
    days_remaining: int = 0
    """距到期天数；已过期时为负。"""

    matched_segments: int = 0

    @property
    def allows_access(self) -> bool:
        return self.state.allows_access

    @property
    def should_warn(self) -> bool:
        """是否需要在前端显示续费提醒。"""
        if self.state is LicenseState.GRACE:
            return True
        return self.state is LicenseState.ACTIVE and self.days_remaining <= WARN_BEFORE_DAYS

    def to_dict(self) -> dict:
        """供 ``/api/license/status`` 序列化。不含授权码原文。"""
        data: dict = {
            "state": self.state.value,
            "allows_access": self.allows_access,
            "should_warn": self.should_warn,
            "message": self.message,
            "days_remaining": self.days_remaining,
            "grace_period_days": GRACE_PERIOD_DAYS,
        }
        if self.payload is not None:
            data.update(
                {
                    "serial": self.payload.serial,
                    "customer": self.payload.customer,
                    "issued": self.payload.issued.isoformat(),
                    "expires": self.payload.expires.isoformat(),
                    "tier": self.payload.tier,
                    "max_users": self.payload.max_users,
                    "features": sorted(self.payload.features),
                }
            )
        return data


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _utcnow().date()


class LicenseManager:
    """授权状态的唯一权威来源。

    单例，挂在 ``app.state.license_manager``。中间件与功能开关都从这里读
    状态。
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / _LICENSE_FILE
        self._lock = threading.RLock()
        self._status = LicenseStatus(
            state=LicenseState.MISSING, message="系统尚未激活"
        )
        self._last_heartbeat_write: datetime | None = None
        self._clock_tampered = False

    # ── 持久化 ────────────────────────────────────────────────────────

    def _read_file(self) -> dict:
        from mclaw.utils.atomic_io import read_json_safe

        data = read_json_safe(self._path)
        return data if isinstance(data, dict) else {}

    def _write_file(self, data: dict) -> None:
        from mclaw.utils.atomic_io import atomic_json_write

        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self._path, data)

    # ── 时钟回拨防护 ──────────────────────────────────────────────────

    def _check_clock(self, stored: dict) -> bool:
        """检测系统时间是否被回拨。

        Returns:
            ``True`` 表示时钟可信。
        """
        raw = stored.get("last_seen_utc")
        if not isinstance(raw, str) or not raw:
            return True
        try:
            last_seen = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("license.json 中 last_seen_utc 格式无效，忽略")
            return True

        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)

        now = _utcnow()
        if now < last_seen - _CLOCK_TOLERANCE:
            logger.error(
                "检测到系统时间回拨: 当前 %s 早于记录的 %s",
                now.isoformat(),
                last_seen.isoformat(),
            )
            return False
        return True

    def touch_heartbeat(self) -> None:
        """更新时钟水位线，按 :data:`_HEARTBEAT_INTERVAL` 节流。

        由中间件在请求路径上调用——必须廉价。绝大多数调用只是比较一个
        内存时间戳后立即返回。
        """
        now = _utcnow()
        with self._lock:
            last = self._last_heartbeat_write
            if last is not None and now - last < _HEARTBEAT_INTERVAL:
                return
            self._last_heartbeat_write = now

        # 写盘放在锁外，避免 I/O 阻塞请求路径上的其他线程。
        try:
            stored = self._read_file()
            stored["last_seen_utc"] = now.isoformat()
            self._write_file(stored)
        except OSError as exc:
            logger.warning("写入授权心跳失败: %s", exc)

    # ── 校验 ──────────────────────────────────────────────────────────

    def _evaluate(self, code: str, *, clock_ok: bool) -> LicenseStatus:
        """对授权码执行完整校验，产出状态。"""
        if not clock_ok:
            return LicenseStatus(
                state=LicenseState.INVALID,
                message="检测到系统时间异常，请校正系统时间后重启",
            )

        try:
            payload = verify_license_code(code)
        except LicenseVerifyError as exc:
            return LicenseStatus(state=LicenseState.INVALID, message=str(exc))

        current = collect_fingerprint()
        matched_ok, matched = fingerprint_matches(payload.fingerprint, current)
        if not matched_ok:
            return LicenseStatus(
                state=LicenseState.MISMATCH,
                payload=payload,
                message=f"授权码与本机不匹配（仅 {matched} 项硬件特征一致）",
                matched_segments=matched,
            )

        today = _today()
        days_remaining = (payload.expires - today).days

        if days_remaining >= 0:
            return LicenseStatus(
                state=LicenseState.ACTIVE,
                payload=payload,
                message="授权有效",
                days_remaining=days_remaining,
                matched_segments=matched,
            )

        overdue = -days_remaining
        if overdue <= GRACE_PERIOD_DAYS:
            return LicenseStatus(
                state=LicenseState.GRACE,
                payload=payload,
                message=(
                    f"授权已于 {payload.expires.isoformat()} 到期，"
                    f"宽限期剩余 {GRACE_PERIOD_DAYS - overdue} 天"
                ),
                days_remaining=days_remaining,
                matched_segments=matched,
            )

        return LicenseStatus(
            state=LicenseState.EXPIRED,
            payload=payload,
            message=f"授权已过期 {overdue} 天，请联系供应商续费",
            days_remaining=days_remaining,
            matched_segments=matched,
        )

    def load(self) -> LicenseStatus:
        """从磁盘加载并完整校验。进程启动时调用一次。"""
        with self._lock:
            stored = self._read_file()
            clock_ok = self._check_clock(stored)
            self._clock_tampered = not clock_ok

            code = stored.get("code")
            if not isinstance(code, str) or not code:
                self._status = LicenseStatus(
                    state=LicenseState.MISSING, message="系统尚未激活"
                )
            else:
                self._status = self._evaluate(code, clock_ok=clock_ok)

            logger.info(
                "授权状态: %s — %s", self._status.state.value, self._status.message
            )
            return self._status

    def activate(self, code: str) -> LicenseStatus:
        """校验并持久化一份新授权码。

        校验不通过时**不写盘**——避免用一个坏码覆盖掉现有的好码。

        Raises:
            LicenseVerifyError: 授权码无效、指纹失配，或本机可用硬件特征
                不足以支撑绑定。
        """
        with self._lock:
            current = collect_fingerprint()
            usable = usable_segment_count(current)
            if usable < MIN_USABLE_SEGMENTS:
                raise LicenseVerifyError(
                    f"本机仅能采集到 {usable} 项硬件特征（需至少 "
                    f"{MIN_USABLE_SEGMENTS} 项），无法完成绑定，请联系技术支持"
                )

            status = self._evaluate(code, clock_ok=not self._clock_tampered)
            if status.state in (LicenseState.INVALID, LicenseState.MISMATCH):
                raise LicenseVerifyError(status.message)
            if status.state is LicenseState.EXPIRED:
                raise LicenseVerifyError(status.message)

            stored = self._read_file()
            stored.update(
                {
                    "code": "".join(code.split()),
                    "activated_at": _utcnow().isoformat(),
                    "last_seen_utc": _utcnow().isoformat(),
                }
            )
            self._write_file(stored)
            self._last_heartbeat_write = _utcnow()
            self._status = status
            logger.info(
                "授权激活成功: %s / %s，到期 %s",
                status.payload.serial if status.payload else "?",
                status.payload.customer if status.payload else "?",
                status.payload.expires.isoformat() if status.payload else "?",
            )
            return status

    # ── 查询 ──────────────────────────────────────────────────────────

    @property
    def status(self) -> LicenseStatus:
        """当前状态快照。

        日期边界会随时间推移改变结论，因此每次读取都用缓存的 payload 重
        新计算日期分支——但不重新验签、不重新采集指纹（两者都只在启动和
        激活时做）。
        """
        with self._lock:
            status = self._status
            if status.payload is None or status.state in (
                LicenseState.INVALID,
                LicenseState.MISMATCH,
                LicenseState.MISSING,
            ):
                return status

            payload = status.payload
            today = _today()
            days_remaining = (payload.expires - today).days
            if days_remaining == status.days_remaining:
                return status

            # 跨过了日期边界，重算状态分支。
            refreshed = self._evaluate_dates(payload, days_remaining, status.matched_segments)
            self._status = refreshed
            return refreshed

    def _evaluate_dates(
        self, payload: LicensePayload, days_remaining: int, matched: int
    ) -> LicenseStatus:
        """仅根据日期重算状态（签名与指纹此前已校验通过）。"""
        if days_remaining >= 0:
            return LicenseStatus(
                state=LicenseState.ACTIVE,
                payload=payload,
                message="授权有效",
                days_remaining=days_remaining,
                matched_segments=matched,
            )
        overdue = -days_remaining
        if overdue <= GRACE_PERIOD_DAYS:
            return LicenseStatus(
                state=LicenseState.GRACE,
                payload=payload,
                message=(
                    f"授权已于 {payload.expires.isoformat()} 到期，"
                    f"宽限期剩余 {GRACE_PERIOD_DAYS - overdue} 天"
                ),
                days_remaining=days_remaining,
                matched_segments=matched,
            )
        return LicenseStatus(
            state=LicenseState.EXPIRED,
            payload=payload,
            message=f"授权已过期 {overdue} 天，请联系供应商续费",
            days_remaining=days_remaining,
            matched_segments=matched,
        )

    def has_feature(self, name: str) -> bool:
        """功能开关是否放行。未激活时一律关闭。"""
        status = self.status
        if not status.allows_access or status.payload is None:
            return False
        return status.payload.has_feature(name)

    def max_users(self) -> int:
        """授权的最大用户数；``0`` 表示不限。未激活时返回 ``1``。"""
        status = self.status
        if not status.allows_access or status.payload is None:
            return 1
        return status.payload.max_users


# ── 全局访问点 ────────────────────────────────────────────────────────
#
# 功能开关的卡点分散在插件/技能/MCP/IM 各处，那些位置拿不到 FastAPI 的
# ``app.state``，因此提供一个模块级单例。

_manager: LicenseManager | None = None
_manager_lock = threading.Lock()


def set_manager(manager: LicenseManager | None) -> None:
    """注册全局实例。由 ``create_app`` 调用。"""
    global _manager
    with _manager_lock:
        _manager = manager


def get_manager() -> LicenseManager | None:
    """返回全局实例；未初始化时为 ``None``。"""
    return _manager


def feature_enabled(name: str, *, default: bool = False) -> bool:
    """功能开关查询，供各卡点调用。

    Args:
        default: 授权系统尚未初始化时的取值。CLI 子命令、单元测试等场景
            不会初始化授权系统，此时不应误伤——由调用方按自身语义决定。
    """
    manager = get_manager()
    if manager is None:
        return default
    return manager.has_feature(name)
