"""Single source of truth for "is the Setup flow required for this request?"

The Setup flow is the first-run UX where a previously unconfigured
:class:`WebAccessConfig` is given a password by an authenticated local user.
Three components consume this state:

- :mod:`mclaw.api.middleware_setup_gate` — returns ``428 setup_required``
  for API calls that hit it before the password is set.
- ``GET /api/auth/setup-status`` — read by the frontend on startup.
- ``POST /api/auth/setup`` and ``POST /api/auth/change-password`` — to allow
  initial password assignment without an existing password.

Keep all setup-state decisions going through these helpers so the rules
stay consistent. Direct ``request.app.state.web_access._data`` access is
forbidden — use :attr:`WebAccessConfig.has_password_set`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .auth import WebAccessConfig

if TYPE_CHECKING:
    from fastapi import Request


__all__ = ["is_setup_complete", "should_require_setup"]


def is_setup_complete(web_access: WebAccessConfig) -> bool:
    """Return ``True`` when a usable password is already stored."""
    return web_access.has_password_set


def should_require_setup(request: Request, web_access: WebAccessConfig) -> bool:
    """Return ``True`` when the caller must complete the Setup flow first.

    Setup is required whenever no password is currently stored, regardless of
    whether the caller is a loopback/desktop connection.  Local requests are
    no longer exempt: the desktop GUI must also complete first-run password
    setup (it logs in with the same credentials afterwards).  This keeps the
    setup gate symmetric with :mod:`mclaw.api.auth`, which removed the
    localhost auth exemption.
    """
    if is_setup_complete(web_access):
        return False
    return True
