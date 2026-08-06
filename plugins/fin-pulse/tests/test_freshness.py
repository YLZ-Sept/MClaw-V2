"""Freshness filtering and title-change tracking tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from finpulse_models import FRESHNESS_DEFAULTS, get_max_age_hours


class TestFreshnessDefaults:
    def test_all_content_types_have_defaults(self) -> None:
        for ct in ("flash", "news", "hot_stock", "policy", "filing", "data", "custom"):
            assert ct in FRESHNESS_DEFAULTS
            assert isinstance(FRESHNESS_DEFAULTS[ct], int)
            assert FRESHNESS_DEFAULTS[ct] > 0

    def test_get_max_age_hours_uses_content_type(self) -> None:
        assert get_max_age_hours("jin10") == FRESHNESS_DEFAULTS["flash"]
        assert get_max_age_hours("nbs") == FRESHNESS_DEFAULTS["data"]
        assert get_max_age_hours("fed_fomc") == FRESHNESS_DEFAULTS["policy"]

    def test_get_max_age_hours_unknown_source(self) -> None:
        result = get_max_age_hours("nonexistent_source_xyz")
        assert result == 72

    def test_flash_shorter_than_news(self) -> None:
        assert FRESHNESS_DEFAULTS["flash"] < FRESHNESS_DEFAULTS["news"]

    def test_hot_stock_shortest(self) -> None:
        assert FRESHNESS_DEFAULTS["hot_stock"] <= FRESHNESS_DEFAULTS["flash"]


class TestSourceDefsFreshness:
    def test_newsnow_sources_have_correct_content_type(self) -> None:
        from finpulse_models import SOURCE_DEFS

        for sid, defn in SOURCE_DEFS.items():
            if defn.get("kind") == "newsnow":
                assert "content_type" in defn, f"{sid} missing content_type"
                assert "newsnow_id" in defn, f"{sid} missing newsnow_id"

    def test_direct_sources_have_content_type(self) -> None:
        from finpulse_models import SOURCE_DEFS

        for sid, defn in SOURCE_DEFS.items():
            if defn.get("kind") == "direct":
                assert "content_type" in defn, f"{sid} missing content_type"

    def test_rss_sources_have_content_type(self) -> None:
        from finpulse_models import SOURCE_DEFS

        for sid, defn in SOURCE_DEFS.items():
            if defn.get("kind") == "rss":
                assert "content_type" in defn, f"{sid} missing content_type"
