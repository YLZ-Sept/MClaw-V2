"""omni-post scheduler — timed publishes + timezone stagger + matrix mode.

Responsibilities
----------------

1. **Ticker loop**: every ~30 s, poll the ``schedules`` table for rows
   whose ``scheduled_at`` has elapsed (including any per-row jitter
   window). Mark the row ``triggered`` and spawn ``run_publish_task``
   via the host's task runtime. The loop is cancellable (``stop()``),
   single-flight, and resilient against transient SQLite lock errors.

2. **Timezone stagger** (``stagger_slots``): given a desired local
   publishing hour (e.g. "20:00 on the user's primary timezone") and a
   list of ``(account, platform)`` pairs, return an ISO-8601 UTC
   ``scheduled_at`` per pair with a small random offset. Accounts in
   the same timezone bucket are offset by ``stagger_seconds`` so we do
   not hammer a single platform with N simultaneous requests.

3. **Matrix fan-out** (``fanout_matrix``): tag-routed dispatching —
   each account may carry ``tags=["food", "travel", ...]`` and a
   matrix request can declare per-tag copy variants. This delegates
   the actual DB writes to the caller so it stays pure.

Design notes
------------

* No external scheduler (croniter / APScheduler) — we keep a minimal
  sleep-based loop. Cron expressions land in S4 if the user actually
  asks for recurring publishes; Sprint 3 only needs one-shot times.
* No dependency on zoneinfo-only Python; we use Python 3.11's stdlib
  ``zoneinfo`` which is already required by ``pyproject.toml``.
* Jitter window defaults to ``schedule_jitter_seconds`` (15 min). An
  account with a ``posting_hour_preference`` tag overrides the global
  hour but only in ``stagger_slots`` — this module never guesses best
  hours, that is MDRM's job (S4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("mclaw.plugins.omni-post")


_POLL_SECONDS = 30.0
_MAX_BATCH = 20


class ScheduleTicker:
    """Background poller that turns scheduled rows into real publishes.

    Built around three collaborators supplied by the plugin loader:

    * ``task_manager`` — :class:`OmniPostTaskManager` with ``list_due_schedules``
      and ``mark_schedule`` methods.
    * ``runner`` — async callable ``(task_id: str) -> None`` that kicks
      off :func:`omni_post_pipeline.run_publish_task`.
    * ``spawn`` — ``api.spawn_task``-compatible helper used to avoid
      blocking the ticker while a publish runs.
    """

    def __init__(
        self,
        *,
        task_manager: Any,
        runner: Callable[[str], Any],
        spawn: Callable[[Any, str | None], Any],
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self._tm = task_manager
        self._runner = runner
        self._spawn = spawn
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    def start(self) -> asyncio.Task[Any]:
        """Idempotent: returns the running ticker task."""

        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="omni-post:scheduler")
        return self._task

    async def stop(self) -> None:
        """Cancel the ticker and wait for clean exit."""

        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None

    async def _run(self) -> None:
        logger.info("omni-post scheduler: started (poll=%ss)", self._poll_seconds)
        try:
            while not self._stop.is_set():
                try:
                    await self._tick_once()
                except Exception:  # noqa: BLE001
                    logger.exception("omni-post scheduler tick errored — backing off")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    continue
        finally:
            logger.info("omni-post scheduler: stopped")

    async def _tick_once(self) -> None:
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = await self._tm.list_due_schedules(now_iso=now_iso, limit=_MAX_BATCH)
        if not rows:
            return
        for row in rows:
            schedule_id = row["id"]
            task_id = row.get("task_id")
            if not task_id:
                await self._tm.mark_schedule(schedule_id, "cancelled")
                continue
            try:
                await self._tm.mark_schedule(schedule_id, "triggered")
                self._spawn(self._runner(task_id), f"omni-post:sched:{task_id}")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "omni-post scheduler: failed to trigger %s (task=%s): %s",
                    schedule_id,
                    task_id,
                    e,
                )


# ── Pure helpers (no I/O) ─────────────────────────────────────────────


def stagger_slots(
    *,
    base_local_hour: int,
    timezone: str,
    accounts: Iterable[dict[str, Any]],
    base_minute: int = 0,
    stagger_seconds: int = 600,
    jitter_seconds: int = 0,
    day_offset: int = 0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return a list of ``{account_id, scheduled_at}`` dicts.

    ``base_local_hour``/``base_minute`` is the desired publish time in
    the author's local ``timezone``. Accounts are staggered so the
    publish queue doesn't fire N parallel submissions into a single
    platform. Accounts in different timezones are all expressed in
    UTC on return, so the caller can persist them directly into
    ``tasks.scheduled_at``.

    Parameters
    ----------
    day_offset
        If the resulting time has already passed, we automatically
        push by +1 day unless ``day_offset >= 0`` pins it. Use
        ``day_offset=0`` + future-hour to keep "post at 20:00 today".
    jitter_seconds
        Each slot gets a random ±jitter to blur fingerprints (issue
        #201 mitigation — platforms flag batches of identical-second
        submissions).
    """

    tz = ZoneInfo(timezone)
    now = now or datetime.now(UTC)
    local_now = now.astimezone(tz)
    slot_date = local_now.date() + timedelta(days=day_offset)
    local_slot = datetime(
        slot_date.year,
        slot_date.month,
        slot_date.day,
        int(base_local_hour),
        int(base_minute),
        tzinfo=tz,
    )
    if local_slot <= local_now and day_offset == 0:
        local_slot += timedelta(days=1)

    slots: list[dict[str, Any]] = []
    for idx, acc in enumerate(accounts):
        offset_seconds = idx * int(stagger_seconds)
        if jitter_seconds:
            offset_seconds += random.randint(-int(jitter_seconds), int(jitter_seconds))
        utc_slot = (local_slot + timedelta(seconds=offset_seconds)).astimezone(UTC)
        slots.append(
            {
                "account_id": acc["id"],
                "platform": acc.get("platform"),
                "scheduled_at": utc_slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return slots


def fanout_matrix(
    *,
    platforms: Iterable[str],
    accounts: Iterable[dict[str, Any]],
    payload: dict[str, Any],
    per_tag_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expand a matrix publish into per-(platform, account) payloads.

    Rules:

    * ``accounts`` whose ``platform`` doesn't match any requested
      platform are silently skipped (the UI already warns about this).
    * For each account, we merge in any ``per_tag_overrides[tag]`` for
      tags the account carries, in the order declared in
      ``accounts[*].tags``. Later tags override earlier ones; explicit
      per-platform overrides in ``payload.per_platform_overrides``
      win last.

    The returned list is ready to be written to ``tasks.payload_json``;
    the scheduler never talks to SQLite itself.
    """

    per_tag_overrides = per_tag_overrides or {}
    wanted = set(platforms)
    per_platform = dict(payload.get("per_platform_overrides") or {})

    out: list[dict[str, Any]] = []
    for acc in accounts:
        pid = acc.get("platform")
        if pid not in wanted:
            continue
        merged = _clone_without_overrides(payload)
        for tag in acc.get("tags") or []:
            if tag in per_tag_overrides:
                merged.update(per_tag_overrides[tag])
        if pid in per_platform:
            merged.update(per_platform[pid])
        out.append(
            {
                "platform": pid,
                "account_id": acc["id"],
                "payload": merged,
            }
        )
    return out


def _clone_without_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    clone = dict(payload)
    clone.pop("per_platform_overrides", None)
    return clone


__all__ = [
    "ScheduleTicker",
    "fanout_matrix",
    "stagger_slots",
]
