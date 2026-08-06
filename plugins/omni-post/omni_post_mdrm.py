"""MDRM (Memory-Driven Recommendation Module) adapter for omni-post.

Purpose
-------
Every time a publish task terminates — success or failure — we want to
leave a small, queryable breadcrumb in the host Memory Manager so that
downstream recommendation logic ("best time to post to platform X for
account Y", "which platforms tend to time out in the morning?") can be
built on top.

We intentionally wrap the four SDK 0.7 host hooks behind this adapter
rather than calling them inline, mirroring the pattern in
``plugins/idea-research/idea_research_inline/mdrm_adapter.py``. The
adapter is **tolerant by design**: if the memory manager is missing, a
permission is denied, or a write raises, we return a ``"skipped"`` /
``"error"`` marker instead of propagating — a broken MDRM must never
prevent a publish from completing.

Memory shape
------------
One record per terminal outcome, written as a ``SemanticMemory`` of type
``EXPERIENCE``:

* ``subject`` — ``"omni-post:publish:{platform}:{account_id}"``
* ``predicate`` — ``"success"`` | ``"failure:{error_kind}"``
* ``content`` — human-readable summary (so the recall path can surface
  it verbatim)
* ``tags`` — structured slugs that downstream aggregators can group by
  (``platform:douyin``, ``account:acc-123``, ``hour:21``, ``weekday:3``,
  ``engine:pw``, ``outcome:success``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mclaw_plugin_sdk import PluginAPI

log = logging.getLogger(__name__)


@dataclass
class MdrmCaps:
    """Reflects which MDRM host services are reachable."""

    has_brain: bool = False
    has_memory_read: bool = False
    has_memory_write: bool = False
    has_vector: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "has_brain": self.has_brain,
            "has_memory_read": self.has_memory_read,
            "has_memory_write": self.has_memory_write,
            "has_vector": self.has_vector,
        }


@dataclass(frozen=True)
class PublishMemoryRecord:
    """Structured view of a single publish outcome we want to remember."""

    task_id: str
    platform: str
    account_id: str
    success: bool
    ts_utc: datetime
    engine: str = "pw"
    error_kind: str | None = None
    asset_kind: str | None = None
    duration_ms: int | None = None
    published_url: str | None = None
    tags_extra: list[str] = field(default_factory=list)

    @property
    def hour(self) -> int:
        return self.ts_utc.astimezone(timezone.utc).hour

    @property
    def weekday(self) -> int:
        return self.ts_utc.astimezone(timezone.utc).weekday()

    def outcome_slug(self) -> str:
        if self.success:
            return "success"
        return f"failure:{self.error_kind or 'unknown'}"

    def subject(self) -> str:
        return f"omni-post:publish:{self.platform}:{self.account_id}"

    def predicate(self) -> str:
        return self.outcome_slug()

    def content(self) -> str:
        verb = "published to" if self.success else "failed to publish to"
        loc = self.published_url or "<no url>"
        when = self.ts_utc.astimezone(timezone.utc).isoformat(timespec="minutes")
        extra = ""
        if not self.success and self.error_kind:
            extra = f" ({self.error_kind})"
        return (
            f"[{when}] account {self.account_id} {verb} {self.platform}"
            f" via {self.engine}: {loc}{extra}"
        )

    def tags(self) -> list[str]:
        base = [
            "omni-post",
            "publish-receipt",
            f"platform:{self.platform}",
            f"account:{self.account_id}",
            f"engine:{self.engine}",
            f"hour:{self.hour}",
            f"weekday:{self.weekday}",
            f"outcome:{'success' if self.success else 'failure'}",
        ]
        if self.asset_kind:
            base.append(f"asset:{self.asset_kind}")
        if self.error_kind:
            base.append(f"error:{self.error_kind}")
        base.extend(t for t in self.tags_extra if t)
        return base


class OmniPostMdrmAdapter:
    """Thin facade over ``api.get_memory_manager()`` (+ vector / brain).

    All public coroutines swallow exceptions and return a status dict so
    callers can safely ``await`` from the publish pipeline without any
    special error handling.
    """

    def __init__(
        self,
        api: Any,
        *,
        plugin_id: str = "omni-post",
    ) -> None:
        self._api = api
        self._plugin_id = plugin_id
        self._memory: Any = None
        self._brain: Any = None
        self._vector: Any = None
        self._caps = MdrmCaps()
        self._detect()

    # -- introspection ---------------------------------------------------

    @property
    def caps(self) -> MdrmCaps:
        return self._caps

    def _has_perm(self, perm: str) -> bool:
        """Side-effect-free permission check.

        IMPORTANT: we intentionally avoid calling the host getters
        (`get_vector_store` / `get_brain` / `get_memory_manager`) when
        the corresponding permission is not granted. Those getters
        mutate the plugin's `_pending_permissions` set via
        `_check_permission`, which surfaces an "approve"
        prompt in the Plugin Manager UI. If a permission is not
        declared in `plugin.json`, any approval would be discarded on
        the next reload (see `_resolve_permissions` in the host
        plugin manager), leading to an infinite "approve → reload →
        re-prompt" loop.
        """
        api = self._api
        if api is None:
            return False
        check = getattr(api, "has_permission", None)
        if check is None:
            # Older SDKs: fall back to best-effort (assume granted so
            # the getters still get a chance). Modern SDKs (>=0.7)
            # always expose has_permission.
            return True
        try:
            return bool(check(perm))
        except Exception as exc:  # noqa: BLE001
            log.debug("has_permission(%s) failed: %s", perm, exc)
            return False

    def _detect(self) -> None:
        api = self._api
        if api is None:
            return
        if self._has_perm("memory.write") or self._has_perm("memory.read"):
            try:
                getter = getattr(api, "get_memory_manager", None)
                self._memory = getter() if getter else None
            except Exception as exc:  # noqa: BLE001
                log.debug("get_memory_manager failed: %s", exc)
                self._memory = None
        self._caps.has_memory_read = self._memory is not None and self._has_perm("memory.read")
        self._caps.has_memory_write = self._memory is not None and self._has_perm("memory.write")

        if self._has_perm("brain.access"):
            try:
                getter = getattr(api, "get_brain", None)
                self._brain = getter() if getter else None
            except Exception as exc:  # noqa: BLE001
                log.debug("get_brain failed: %s", exc)
                self._brain = None
        self._caps.has_brain = self._brain is not None

        if self._has_perm("vector.access"):
            try:
                getter = getattr(api, "get_vector_store", None)
                self._vector = getter() if getter else None
            except Exception as exc:  # noqa: BLE001
                log.debug("get_vector_store failed: %s", exc)
                self._vector = None
        self._caps.has_vector = self._vector is not None

    # -- writes ----------------------------------------------------------

    async def write_publish_memory(self, record: PublishMemoryRecord) -> dict[str, str]:
        """Persist one publish outcome into the host Memory Manager.

        Returns ``{"status": "ok" | "skipped" | "error", ...}``. Never
        raises — the caller is the hot publish pipeline.
        """

        if not self._caps.has_memory_write or self._memory is None:
            return {"status": "skipped", "reason": "no_memory_manager"}

        try:
            memory_obj = self._build_memory_object(record)
        except Exception as exc:  # noqa: BLE001
            log.debug("build memory object failed: %s", exc)
            return {"status": "error", "reason": "build_failed"}

        if memory_obj is None:
            return {"status": "skipped", "reason": "unsupported_sdk"}

        try:
            add = getattr(self._memory, "add_memory", None)
            if add is None:
                return {"status": "skipped", "reason": "no_add_memory"}
            result = add(memory_obj, scope="global", scope_owner=self._plugin_id)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            return {"status": "ok", "memory_id": str(result or "")}
        except TypeError:
            try:
                result = self._memory.add_memory(memory_obj)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
                return {"status": "ok", "memory_id": str(result or "")}
            except Exception as exc:  # noqa: BLE001
                log.debug("add_memory fallback failed: %s", exc)
                return {"status": "error", "reason": exc.__class__.__name__}
        except Exception as exc:  # noqa: BLE001
            log.debug("add_memory failed: %s", exc)
            return {"status": "error", "reason": exc.__class__.__name__}

    # -- helpers ---------------------------------------------------------

    def _build_memory_object(self, record: PublishMemoryRecord) -> Any:
        """Construct a ``SemanticMemory``-compatible object.

        We import lazily inside the adapter so that omni-post can still
        load in environments where the host's memory module isn't
        available (e.g., pure unit tests running against a bare SDK
        stub). When the import fails we fall back to a tagged dict —
        most memory-manager stubs accept a dict.
        """
        tags = record.tags()
        content = record.content()
        subject = record.subject()
        predicate = record.predicate()
        now = datetime.now(timezone.utc)

        try:
            from mclaw.memory.types import (
                MemoryPriority,
                MemoryType,
                SemanticMemory,
            )

            return SemanticMemory(
                type=MemoryType.EXPERIENCE,
                priority=MemoryPriority.LONG_TERM,
                content=content,
                subject=subject,
                predicate=predicate,
                tags=tags,
                source="omni-post",
                created_at=record.ts_utc,
                updated_at=now,
                importance_score=0.55 if record.success else 0.7,
            )
        except Exception:
            return {
                "type": "experience",
                "priority": "long_term",
                "subject": subject,
                "predicate": predicate,
                "content": content,
                "tags": tags,
                "source": "omni-post",
                "created_at": record.ts_utc.isoformat(),
                "updated_at": now.isoformat(),
                "scope": "global",
                "scope_owner": self._plugin_id,
            }


__all__ = [
    "MdrmCaps",
    "OmniPostMdrmAdapter",
    "PublishMemoryRecord",
]
