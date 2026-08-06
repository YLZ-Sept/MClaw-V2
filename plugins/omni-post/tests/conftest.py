"""Test bootstrap — make plugin modules importable with bare names.

Both the plugin itself and the unit tests use short imports like
``from omni_post_models import PublishRequest`` rather than a
``plugins.omni_post.omni_post_models`` path. This conftest puts the
plugin directory at the front of sys.path so pytest picks up those
bare imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
