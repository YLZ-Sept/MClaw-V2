"""Profile-scoped isolated MemoryManagers for org nodes.

Direct profile chats get their isolated manager from
``AgentFactory._apply_memory_isolation``. Org nodes execute through the main
Agent's shared handler registry, so they need the same profile-scoped manager
to avoid reading/writing the main Agent's (or another role's) memory.

Paths and ``agent_id`` mirror ``AgentFactory._apply_memory_isolation`` so a
role's org-node memory and its direct-chat memory live in the same per-profile
store, while different roles remain strictly separated.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_managers: dict[str, Any] = {}
_lock = threading.RLock()


def _isolated_memory_md_seed(profile: Any) -> str:
    name = (getattr(profile, "name", "") or getattr(profile, "id", "")).strip()
    return (
        f"# {name} — 独立记忆\n\n"
        f"<!-- seeded {datetime.now().isoformat(timespec='seconds')} -->\n\n"
        f"_这是 Agent `{profile.id}` 的独立记忆文件。_\n\n"
        "该 Agent 配置了 `memory_isolation=isolated`，拥有独立的偏好、经验和"
        "事实记忆，不会和其它 Agent 或全局记忆混在一起。_无需手动编辑。_\n"
    )


def build_isolated_memory_manager(profile: Any, brain: Any = None) -> Any:
    """Construct a fresh isolated MemoryManager for one AgentProfile."""
    from mclaw.agent.user_profile import get_profile_manager
    from mclaw.agents.profile import get_profile_store
    from mclaw.config import settings
    from mclaw.memory.manager import MemoryManager

    store = get_profile_store()
    profile_dir = store.ensure_profile_dir(profile.id)
    memory_dir = profile_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_md_path = profile_dir / "identity" / "MEMORY.md"
    if not memory_md_path.exists():
        try:
            memory_md_path.parent.mkdir(parents=True, exist_ok=True)
            memory_md_path.write_text(
                _isolated_memory_md_seed(profile), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Memory] Failed to seed isolated MEMORY.md for %s: %s", profile.id, exc
            )
            memory_md_path = settings.memory_path

    manager = MemoryManager(
        data_dir=memory_dir,
        memory_md_path=memory_md_path,
        identity_dir=profile_dir / "identity",
        brain=brain,
        embedding_model=settings.embedding_model,
        embedding_device=settings.embedding_device,
        model_download_source=settings.model_download_source,
        search_backend=settings.search_backend,
        embedding_api_provider=settings.embedding_api_provider,
        embedding_api_key=settings.embedding_api_key,
        embedding_api_model=settings.embedding_api_model,
        agent_id=profile.id,
    )
    try:
        manager.profile_manager = get_profile_manager()
    except Exception:  # noqa: BLE001 — profile manager is optional
        pass
    return manager


def get_isolated_memory_manager(profile_id: str | None, brain: Any = None) -> Any | None:
    """Return a cached isolated manager for an org node's profile.

    Returns ``None`` for unknown profiles or shared-memory profiles, in which
    case callers fall back to the main Agent's manager.
    """
    if not profile_id:
        return None
    from mclaw.agents.profile import get_profile_store

    profile = get_profile_store().get(profile_id)
    if profile is None or getattr(profile, "memory_mode", "shared") != "isolated":
        return None

    with _lock:
        cached = _managers.get(profile_id)
        if cached is not None:
            return cached
        manager = build_isolated_memory_manager(profile, brain)
        _managers[profile_id] = manager
        logger.info(
            "[Memory] org-node isolated manager ready: profile=%s agent_id=%s",
            profile_id,
            getattr(manager, "agent_id", ""),
        )
        return manager
