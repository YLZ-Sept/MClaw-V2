"""omni-post PlatformAdapter abstraction.

The engine layer (``omni_post_engine_pw.py``) drives each adapter via
three life-cycle methods:

    await adapter.precheck(context)   # verify cookies, warm up
    await adapter.fill_form(context)  # upload file + fill caption/tags
    await adapter.submit(context)     # click publish + wait for result

All DOM access goes through :meth:`find_input` on the Page wrapper —
adapters MUST NOT call ``page.locator`` directly. This is how we keep
hardcoded selectors out of code and into JSON where the self-heal
probe can replace them on the fly.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("mclaw.plugins.omni-post")


@dataclass
class AdapterContext:
    """One-shot context passed to every adapter method.

    The engine creates it per ``(task, account)`` pair; the adapter may
    mutate ``screenshots`` to register intermediate captures but MUST
    NOT rely on in-process state persisting between calls.
    """

    task_id: str
    platform: str
    account_id: str
    account_nickname: str
    payload: dict[str, Any]
    asset_storage_path: str
    cover_storage_path: str | None = None
    auto_submit: bool = True
    screenshots: list[str] = field(default_factory=list)
    # The engine populates these before calling the adapter.
    page: Any = None  # playwright.async_api.Page, typed as Any to avoid a runtime import
    selectors: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterOutcome:
    """Result of a full adapter run.

    The engine translates this into a ``tasks`` row update + a
    ``publish_receipt`` Asset Bus write.
    """

    success: bool
    published_url: str | None = None
    error_kind: str | None = None
    error_message: str = ""
    screenshots: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


# ── Selector bundle ─────────────────────────────────────────────────


def load_selector_bundle(platform_id: str, selectors_dir: Path) -> dict[str, Any]:
    """Read and validate one platform's selector JSON.

    The JSON file is the *only* source of truth for DOM paths — adapters
    use :meth:`PlatformAdapter.find_input` to resolve a named key to a
    locator at runtime. This keeps the code free of hard-coded strings
    and lets the self-heal probe update a file without a plugin rebuild.
    """

    path = selectors_dir / f"{platform_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"missing selector bundle: {path}")
    with path.open("r", encoding="utf-8") as f:
        bundle = json.load(f)
    _validate_bundle(bundle, platform_id)
    return bundle


def _validate_bundle(bundle: dict, platform_id: str) -> None:
    if not isinstance(bundle, dict):
        raise ValueError(f"{platform_id}.json must be a JSON object")
    if "platform" not in bundle:
        raise ValueError(f"{platform_id}.json is missing 'platform' key")
    if bundle["platform"] != platform_id:
        raise ValueError(f"{platform_id}.json declares platform={bundle['platform']!r}")
    if "actions" not in bundle or not isinstance(bundle["actions"], dict):
        raise ValueError(f"{platform_id}.json must declare an 'actions' object")
    for action_name, action in bundle["actions"].items():
        if not isinstance(action, dict):
            raise ValueError(f"{platform_id}.json action {action_name!r} must be an object")
        # `url` is optional — actions like `submit` piggyback on the
        # page opened by a previous action (e.g. `fill_form`) and do not
        # navigate. The engine only calls page.goto when `url` is set.
        if "url" in action and not isinstance(action["url"], str):
            raise ValueError(f"{platform_id}.json action {action_name!r}.url must be a string")
        steps = action.get("steps", [])
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"{platform_id}.json {action_name}.steps[{i}] must be an object")
            kind = step.get("kind")
            if kind not in {
                "wait",
                "click",
                "type",
                "upload",
                "select",
                "assert_url",
                "eval",
                "shadow_click",
                "shadow_upload",
            }:
                raise ValueError(
                    f"{platform_id}.json {action_name}.steps[{i}].kind={kind!r}"
                    " is not a known step kind"
                )


# ── Adapter ABC ─────────────────────────────────────────────────────


class PlatformAdapter(ABC):
    """Abstract adapter. One concrete subclass per platform.

    The default implementation in :mod:`omni_post_engine_pw` already
    handles 95% of platforms by interpreting a ``steps`` array in the
    selector JSON. Subclassing is only required when a platform needs
    custom logic that doesn't map to the 9 step kinds, e.g. WeChat
    Channels (shadow DOM) or WeChat MP (markdown editor).
    """

    platform_id: str = ""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self._bundle = bundle

    @property
    def bundle(self) -> dict[str, Any]:
        return self._bundle

    def resolve_action(self, action_name: str) -> dict[str, Any]:
        """Return the ``actions.<name>`` block or raise KeyError."""

        try:
            return self._bundle["actions"][action_name]
        except KeyError as e:
            raise KeyError(
                f"{self.platform_id} selector bundle has no action {action_name!r}"
            ) from e

    @abstractmethod
    async def precheck(self, ctx: AdapterContext) -> AdapterOutcome:
        """Navigate to the upload page and verify login state.

        A failure here (HTTP 302, 401, login modal detected) MUST map to
        :class:`ErrorKind.cookie_expired` — no retry, no submit.
        """

    @abstractmethod
    async def fill_form(self, ctx: AdapterContext) -> AdapterOutcome:
        """Upload the asset and fill title / description / tags."""

    @abstractmethod
    async def submit(self, ctx: AdapterContext) -> AdapterOutcome:
        """Click publish; return the final URL when one is available."""


__all__ = [
    "AdapterContext",
    "AdapterOutcome",
    "PlatformAdapter",
    "load_selector_bundle",
]
