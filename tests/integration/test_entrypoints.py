"""Smoke tests for top-level entry points.

P8.7-fix added these after G-RC-8 audit caught that main.py and mcp_server.py
still imported from the deleted core.agent shim (a regression that escaped
P-RC-7 because these modules were not in the gate selector).

These tests assert that the CLI and MCP server modules can be imported
without raising, guarding against future shim-deletion drift.
"""

import importlib
import shutil
import subprocess
import sys


def test_mclaw_main_imports():
    """src/mclaw/main.py must import without ImportError after shim deletion."""
    importlib.import_module("mclaw.main")


def test_mclaw_mcp_server_imports():
    """src/mclaw/mcp_server.py must import without ImportError after shim deletion."""
    importlib.import_module("mclaw.mcp_server")


def test_mclaw_cli_help_smoke():
    """`mclaw --help` must exit 0 (catches missing console script entry).

    Prefers ``python -m mclaw`` (uses ``src/mclaw/__main__.py``) so we
    do not depend on the installed console script being on PATH. Falls back
    to the installed ``mclaw`` executable via ``shutil.which`` for
    environments where ``__main__.py`` is missing.
    """
    cmd = [sys.executable, "-m", "mclaw", "--help"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        exe = shutil.which("mclaw")
        assert exe is not None, (
            "neither `python -m mclaw` nor `mclaw` console script available"
        )
        result = subprocess.run(
            [exe, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )

    assert result.returncode == 0, (
        f"mclaw --help exited {result.returncode}\n"
        f"stdout: {result.stdout[:500]}\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "mclaw" in (result.stdout + result.stderr).lower()
