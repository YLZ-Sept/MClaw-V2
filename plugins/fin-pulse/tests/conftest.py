"""Shared pytest fixtures for the fin-pulse plugin test suite.

The plugin directory is added to ``sys.path`` so intra-package imports
like ``from finpulse_task_manager import FinpulseTaskManager`` behave
the same way they do when the host PluginManager loads the plugin
(the loader prepends the plugin dir to ``sys.path`` at load time).
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
