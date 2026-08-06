"""Selector self-healing probe for omni-post.

Why
---
Every platform adapter relies on a set of DOM selectors (see
``omni_post_engine_pw.py`` and the JSON bundles under
``omni_post_selectors_bundle/``). When a platform ships a redesign the
selectors silently rot; the user only notices when a publish fails hours
or days later.

The self-heal cycle fixes that by probing each selector on a schedule
(usually once per day) against a lightweight page snapshot and updating
``selectors_health`` rows in the plugin DB. When the hit-rate for a
platform drops below ``ALERT_THRESHOLD`` we fire an IM notification via
the plugin's broadcast bus — but only at most once per
``ALERT_COOLDOWN`` to avoid paging fatigue.

Design notes
------------
* We intentionally keep the probe function pluggable (``probe_fn``) so
  the core logic can be unit-tested without spinning up a real browser.
  Production wires a Playwright-backed probe; tests wire a deterministic
  fake.
* Alert throttling reads ``last_alerted_at`` from the existing
  ``selectors_health`` row (populated by ``mark_selector_alerted``), so
  state survives process restarts.
* The ticker is opt-in: the plugin only starts it when
  ``settings.enable_selfheal`` is true (falls back to on).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

log = logging.getLogger(__name__)

ALERT_THRESHOLD = 0.6
"""Hit rate below this triggers an alert (60 %)."""

ALERT_COOLDOWN = timedelta(hours=24)
"""Don't re-alert the same platform more often than this."""

DEFAULT_INTERVAL_HOURS = 24.0
"""Default cadence for :class:`SelfHealTicker` — once per day."""


@dataclass(frozen=True)
class SelectorProbeResult:
    """Outcome of probing one platform's selector bundle."""

    platform: str
    hit_rate: float
    total: int
    failed: int
    last_error: str | None


ProbeFn = Callable[[str, str, Any], Awaitable[bool]]
"""``(platform, selector_key, selector_spec) -> bool`` — True if resolvable."""

Notifier = Callable[[str, dict[str, Any]], Awaitable[None]]
"""``(platform, payload) -> None`` — IM / webhook dispatcher."""


class _TaskManagerLike(Protocol):
    async def record_selector_health(
        self,
        *,
        platform: str,
        hit_rate: float,
        total_probes: int,
        failed_probes: int,
        last_error: str | None = None,
    ) -> None: ...

    async def mark_selector_alerted(self, platform: str) -> None: ...

    async def list_selector_health(self) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Core routines
# ---------------------------------------------------------------------------


async def probe_platform(
    platform: str,
    selectors: Mapping[str, Any],
    *,
    probe_fn: ProbeFn,
) -> SelectorProbeResult:
    """Probe every selector in *selectors* and aggregate the hit rate.

    Exceptions raised by the probe function count as *misses* — we never
    let a broken platform kill the whole cycle.
    """
    total = 0
    failed = 0
    last_error: str | None = None
    for key, spec in selectors.items():
        total += 1
        ok = False
        try:
            ok = bool(await probe_fn(platform, key, spec))
        except Exception as exc:  # noqa: BLE001 — we log and continue
            last_error = f"{key}: {exc.__class__.__name__}: {exc}"
            log.debug("probe %s/%s raised %r", platform, key, exc)
        if not ok:
            failed += 1
            last_error = last_error or f"{key}: unresolved"
    hit_rate = 1.0 if total == 0 else (total - failed) / total
    return SelectorProbeResult(
        platform=platform,
        hit_rate=hit_rate,
        total=total,
        failed=failed,
        last_error=last_error,
    )


async def run_probe_cycle(
    *,
    selectors_by_platform: Mapping[str, Mapping[str, Any]],
    task_manager: _TaskManagerLike,
    probe_fn: ProbeFn,
    notifier: Notifier | None = None,
    alert_threshold: float = ALERT_THRESHOLD,
    alert_cooldown: timedelta = ALERT_COOLDOWN,
    now_fn: Callable[[], datetime] | None = None,
) -> list[SelectorProbeResult]:
    """Run a full probe cycle across all registered platforms.

    Returns one :class:`SelectorProbeResult` per platform. Side effects:

    * writes each result into ``selectors_health`` via the task manager;
    * if hit_rate < ``alert_threshold`` **and** the last alert is older
      than ``alert_cooldown``, fires the notifier and stamps
      ``last_alerted_at``.
    """
    now = now_fn or (lambda: datetime.now(timezone.utc))
    results: list[SelectorProbeResult] = []
    try:
        existing_rows = await task_manager.list_selector_health()
    except Exception:  # noqa: BLE001
        existing_rows = []
    existing = {row.get("platform"): row for row in existing_rows if row.get("platform")}

    for platform, selectors in selectors_by_platform.items():
        result = await probe_platform(platform, selectors, probe_fn=probe_fn)
        try:
            await task_manager.record_selector_health(
                platform=result.platform,
                hit_rate=result.hit_rate,
                total_probes=result.total,
                failed_probes=result.failed,
                last_error=result.last_error,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("record_selector_health failed for %s: %s", platform, exc)

        if notifier is not None and result.hit_rate < alert_threshold:
            prev = existing.get(platform) or {}
            last_alert = prev.get("last_alerted_at")
            due = True
            if last_alert:
                try:
                    iso = str(last_alert).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(iso)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    due = (now() - dt) >= alert_cooldown
                except Exception:
                    due = True
            if due:
                payload = {
                    "platform": result.platform,
                    "hit_rate": round(result.hit_rate, 3),
                    "total": result.total,
                    "failed": result.failed,
                    "last_error": result.last_error,
                    "threshold": alert_threshold,
                }
                try:
                    await notifier(platform, payload)
                except Exception as exc:  # noqa: BLE001
                    log.warning("selfheal notifier raised for %s: %s", platform, exc)
                else:
                    with contextlib.suppress(Exception):
                        await task_manager.mark_selector_alerted(platform)

        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Background ticker
# ---------------------------------------------------------------------------


class SelfHealTicker:
    """Fires :func:`run_probe_cycle` on a fixed interval."""

    def __init__(
        self,
        *,
        selectors_by_platform: Mapping[str, Mapping[str, Any]],
        task_manager: _TaskManagerLike,
        probe_fn: ProbeFn,
        notifier: Notifier | None = None,
        interval_hours: float = DEFAULT_INTERVAL_HOURS,
    ) -> None:
        self._cfg = selectors_by_platform
        self._tm = task_manager
        self._probe = probe_fn
        self._notifier = notifier
        self._interval_seconds = max(60.0, float(interval_hours) * 3600.0)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await run_probe_cycle(
                    selectors_by_platform=self._cfg,
                    task_manager=self._tm,
                    probe_fn=self._probe,
                    notifier=self._notifier,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("selfheal cycle raised: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue

    def start(self, spawn: Callable[[Any, str | None], Any]) -> None:
        if self._task is None:
            self._task = spawn(self._loop(), "omni-post-selfheal")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except Exception:
                self._task.cancel()
                with contextlib.suppress(Exception):
                    await self._task
            self._task = None


__all__ = [
    "ALERT_COOLDOWN",
    "ALERT_THRESHOLD",
    "DEFAULT_INTERVAL_HOURS",
    "Notifier",
    "ProbeFn",
    "SelectorProbeResult",
    "SelfHealTicker",
    "probe_platform",
    "run_probe_cycle",
]
