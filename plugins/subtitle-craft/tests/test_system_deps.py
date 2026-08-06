"""Browser detection and whitelisted installer coverage for subtitle-craft."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from subtitle_craft_inline import system_deps


def test_detects_system_chromium_from_path(monkeypatch):
    manager = system_deps.SystemDepsManager()
    browser = "/usr/bin/chromium-browser"

    monkeypatch.setattr(
        system_deps.shutil,
        "which",
        lambda probe: browser if probe == "chromium-browser" else None,
    )
    monkeypatch.setattr(
        system_deps.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="Chromium 143.0.7499.40 Built on NanoPC-T6\n",
            stderr="",
        ),
    )

    snapshot = manager.detect("system-chromium")

    assert snapshot["found"] is True
    assert snapshot["location"] == browser
    assert snapshot["version"] == "143.0.7499.40"


def test_detects_playwright_browser_from_configured_cache(tmp_path: Path, monkeypatch):
    executable = (
        tmp_path
        / "chromium_headless_shell-1228"
        / "chrome-linux"
        / "headless_shell"
    )
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))

    snapshot = system_deps.SystemDepsManager().detect("playwright-chromium")

    assert snapshot["found"] is True
    assert snapshot["location"] == str(executable)


def test_playwright_installer_command_is_fixed_to_current_runtime():
    methods = system_deps.SystemDepsManager().methods("playwright-chromium")

    assert len(methods) == 1
    command = methods[0]["command_hint"]
    assert command.endswith("-m playwright install chromium")
    assert methods[0]["requires_sudo"] is False
