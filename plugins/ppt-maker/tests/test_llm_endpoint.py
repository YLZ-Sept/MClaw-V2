from __future__ import annotations

import json

from ppt_brain_adapter import PptBrainAdapter


class FakeAPI:
    def __init__(self, config: dict[str, str]) -> None:
        self.config = config

    def get_config(self) -> dict[str, str]:
        return self.config


def test_central_selector_endpoint_overrides_legacy_settings_file(tmp_path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"llm_endpoint": "legacy-model"}),
        encoding="utf-8",
    )
    adapter = PptBrainAdapter(
        FakeAPI({"llm_endpoint": "selected-model"}),
        data_root=tmp_path,
    )

    assert adapter._endpoint() == "selected-model"


def test_explicit_central_inherit_overrides_legacy_settings_file(tmp_path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"llm_endpoint": "legacy-model"}),
        encoding="utf-8",
    )
    adapter = PptBrainAdapter(FakeAPI({"llm_endpoint": ""}), data_root=tmp_path)

    assert adapter._endpoint() == ""
