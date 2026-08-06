"""Unit tests for finpulse_services.radar_library (Phase 6b).

Uses a minimal in-memory ``_FakeStore`` double implementing the
two-method protocol declared by :mod:`finpulse_services.radar_library`.
This keeps the tests fast and disconnected from SQLite — the module is
already covered end-to-end via the REST route integration, so we only
need to exercise the pure logic here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from finpulse_services.radar_library import (
    MAX_PRESETS,
    MAX_RULES_BYTES,
    delete_preset,
    list_presets,
    save_preset,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeStore:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    async def get_config(self, key: str) -> str:
        return self.data.get(key, "")

    async def set_configs(self, updates: dict[str, str]) -> None:
        self.data.update(updates)


class TestListPresets:
    def test_empty_store_returns_empty_list(self) -> None:
        out = _run(list_presets(_FakeStore()))
        assert out == []

    def test_malformed_json_is_swallowed(self) -> None:
        store = _FakeStore({"radar_rules_library": "not-json"})
        assert _run(list_presets(store)) == []

    def test_non_list_payload_is_swallowed(self) -> None:
        store = _FakeStore({"radar_rules_library": json.dumps({"name": "oops"})})
        assert _run(list_presets(store)) == []

    def test_drops_invalid_entries(self) -> None:
        payload = json.dumps(
            [
                {"name": "keep", "rules_text": "+a", "saved_at": "2026-01-01T00:00:00Z"},
                {"name": "", "rules_text": "+b"},
                "bad",
                {"no_name": True},
            ]
        )
        store = _FakeStore({"radar_rules_library": payload})
        out = _run(list_presets(store))
        assert [it["name"] for it in out] == ["keep"]


class TestSavePreset:
    def test_happy_path_inserts_newest_first(self) -> None:
        store = _FakeStore()
        _run(save_preset(store, name="rule-a", rules_text="+a", now_iso="2026-01-01T00:00:00Z"))
        _run(save_preset(store, name="rule-b", rules_text="+b", now_iso="2026-01-02T00:00:00Z"))
        out = _run(list_presets(store))
        assert [it["name"] for it in out] == ["rule-b", "rule-a"]

    def test_upsert_replaces_in_place(self) -> None:
        store = _FakeStore()
        _run(save_preset(store, name="dup", rules_text="+v1", now_iso="2026-01-01T00:00:00Z"))
        _run(save_preset(store, name="other", rules_text="+x", now_iso="2026-01-02T00:00:00Z"))
        _run(save_preset(store, name="dup", rules_text="+v2", now_iso="2026-01-03T00:00:00Z"))
        out = _run(list_presets(store))
        assert [it["name"] for it in out] == ["dup", "other"]
        assert out[0]["rules_text"] == "+v2"
        assert out[0]["saved_at"] == "2026-01-03T00:00:00Z"

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            _run(save_preset(_FakeStore(), name="  ", rules_text="+a"))

    def test_rejects_empty_rules(self) -> None:
        with pytest.raises(ValueError, match="rules_text"):
            _run(save_preset(_FakeStore(), name="x", rules_text=""))

    def test_rejects_oversized_rules(self) -> None:
        oversize = "+" + ("a" * MAX_RULES_BYTES)
        with pytest.raises(ValueError, match="too large"):
            _run(save_preset(_FakeStore(), name="big", rules_text=oversize))

    def test_caps_at_max_presets(self) -> None:
        store = _FakeStore()
        for i in range(MAX_PRESETS + 5):
            _run(
                save_preset(
                    store,
                    name=f"r{i:03d}",
                    rules_text=f"+k{i}",
                    now_iso=f"2026-01-01T00:{i % 60:02d}:00Z",
                )
            )
        out = _run(list_presets(store))
        assert len(out) == MAX_PRESETS
        # Newest-first ⇒ the last saved name should be at index 0.
        assert out[0]["name"] == f"r{MAX_PRESETS + 4:03d}"

    def test_persists_as_valid_json(self) -> None:
        store = _FakeStore()
        _run(save_preset(store, name="a", rules_text="+x"))
        blob = store.data["radar_rules_library"]
        parsed = json.loads(blob)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "a"
        assert parsed[0]["rules_text"] == "+x"


class TestDeletePreset:
    def test_returns_true_on_hit(self) -> None:
        store = _FakeStore()
        _run(save_preset(store, name="kill", rules_text="+a"))
        assert _run(delete_preset(store, "kill")) is True
        assert _run(list_presets(store)) == []

    def test_returns_false_on_miss(self) -> None:
        store = _FakeStore()
        _run(save_preset(store, name="keep", rules_text="+a"))
        assert _run(delete_preset(store, "ghost")) is False
        assert len(_run(list_presets(store))) == 1

    def test_returns_false_on_empty_name(self) -> None:
        assert _run(delete_preset(_FakeStore(), "   ")) is False
