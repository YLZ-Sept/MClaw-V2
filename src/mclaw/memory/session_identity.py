"""Canonical derivation of memory session ids and owners.

A raw session key (``namespace:chat_id:user_id``) is used as a SQLite
``session_id`` and as part of on-disk paths, so it must be collapsed into a
filesystem/SQL-safe form.  That rule used to be copy-pasted at every call
site, with the consistency kept by hand ("must match the logic in ...").  It
lives here instead, so all readers and writers agree by construction.
"""

from __future__ import annotations

import re
from typing import Any

DESKTOP_USER_ID = "desktop_user"

# ``:`` is expanded to ``__`` first so the id stays readable, then everything
# in this class collapses to a single ``_``.
_UNSAFE_ID_CHARS = re.compile(r'[/\\+=%?*<>|"\x00-\x1f]')


def sanitize_session_id(raw: str | None) -> str:
    """Collapse a raw session key into a filesystem/SQL-safe identifier."""
    return _UNSAFE_ID_CHARS.sub("_", (raw or "").replace(":", "__"))


def memory_session_id(session: Any | None, fallback: str = "") -> str:
    """Safe session id derived from ``session``'s key, or ``fallback``."""
    return sanitize_session_id(getattr(session, "session_key", None) or fallback)
