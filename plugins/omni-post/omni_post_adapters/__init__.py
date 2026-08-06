"""omni-post platform adapters — one subpackage per platform.

Adapters follow a single abstract contract (see :mod:`.base`) so the
pipeline can iterate over them without caring about DOM details. For
platforms whose UI is stable (most of them) we only need a selectors
JSON under ``omni_post_selectors/<platform>.json`` and a thin wrapper
class here; for micro-frontend platforms (WeChat Channels) we ship a
dedicated adapter that overrides ``fill_form`` / ``submit`` to walk the
shadow DOM and iframe tree.
"""

from .base import AdapterContext, AdapterOutcome, PlatformAdapter, load_selector_bundle

__all__ = [
    "AdapterContext",
    "AdapterOutcome",
    "PlatformAdapter",
    "load_selector_bundle",
]
