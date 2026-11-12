"""Per-node Agent context (profile id + memory manager).

Org nodes execute tools through the main desktop Agent's handler registry.
That registry's KnowledgeHandler / MemoryHandler normally bind to the main
Agent's profile and shared memory manager. This context lets the org runtime
temporarily scope those handlers to the node's AgentProfile and its isolated
MemoryManager without cloning the whole handler registry per node.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_current_profile_id: ContextVar[str | None] = ContextVar(
    "mclaw_agent_profile_id", default=None
)
_current_memory_manager: ContextVar[Any | None] = ContextVar(
    "mclaw_agent_memory_manager", default=None
)


def set_current_agent_context(
    *,
    profile_id: str | None = None,
    memory_manager: Any | None = None,
) -> tuple[Token, Token]:
    """Install the current node's profile and memory manager. Returns reset tokens."""
    return (
        _current_profile_id.set(profile_id),
        _current_memory_manager.set(memory_manager),
    )


def reset_current_agent_context(tokens: tuple[Token, Token]) -> None:
    profile_token, memory_token = tokens
    _current_memory_manager.reset(memory_token)
    _current_profile_id.reset(profile_token)


def get_current_agent_profile_id() -> str | None:
    return _current_profile_id.get()


def get_current_memory_manager() -> Any | None:
    return _current_memory_manager.get()
