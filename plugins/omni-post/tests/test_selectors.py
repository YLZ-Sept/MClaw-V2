"""Validate every shipped selector JSON can be parsed by the loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from omni_post_adapters import load_selector_bundle

SELECTOR_DIR = Path(__file__).resolve().parent.parent / "omni_post_selectors"


@pytest.mark.parametrize("platform", [p.stem for p in SELECTOR_DIR.glob("*.json")])
def test_bundle_loads(platform: str) -> None:
    bundle = load_selector_bundle(platform, SELECTOR_DIR)
    assert bundle["platform"] == platform
    assert "actions" in bundle
    for action_name in ("precheck", "fill_form", "submit"):
        assert action_name in bundle["actions"], f"{platform}.json missing action {action_name!r}"
