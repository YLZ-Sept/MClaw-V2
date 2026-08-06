from __future__ import annotations

import json

import pytest
from plugin import ClarifyRequest, Plugin


class FakeLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return type("Completion", (), {"text": json.dumps(self.response, ensure_ascii=False)})()


class FakeAPI:
    def __init__(
        self,
        llm: FakeLLM,
        endpoint: str = "",
        central_endpoint: str | None = None,
    ) -> None:
        self.llm = llm
        self.config = {"excel_maker_settings": {"llm_endpoint": endpoint}}
        if central_endpoint is not None:
            self.config["llm_endpoint"] = central_endpoint

    def get_llm(self):
        return self.llm

    def get_config(self):
        return self.config


@pytest.mark.asyncio
async def test_clarification_uses_required_configured_endpoint(tmp_path):
    llm = FakeLLM({"questions": ["主要读者是谁？"]})
    plugin = Plugin()
    plugin._api = FakeAPI(llm, endpoint="finance-local")
    plugin._data_dir = tmp_path

    questions = await plugin._clarify_questions(ClarifyRequest(goal="生成月报"))

    assert questions == ["主要读者是谁？"]
    assert llm.calls[0]["endpoint"] == "finance-local"
    assert llm.calls[0]["policy"] == "require"


@pytest.mark.asyncio
async def test_clarification_uses_inherit_when_endpoint_is_empty(tmp_path):
    llm = FakeLLM({"questions": ["统计周期是什么？"]})
    plugin = Plugin()
    plugin._api = FakeAPI(llm)
    plugin._data_dir = tmp_path

    await plugin._clarify_questions(ClarifyRequest(goal="生成月报"))

    assert llm.calls[0]["policy"] == "inherit"
    assert "endpoint" not in llm.calls[0]


@pytest.mark.asyncio
async def test_central_selector_endpoint_overrides_legacy_nested_setting(tmp_path):
    llm = FakeLLM({"questions": ["统计周期是什么？"]})
    plugin = Plugin()
    plugin._api = FakeAPI(
        llm,
        endpoint="legacy-model",
        central_endpoint="selected-model",
    )
    plugin._data_dir = tmp_path

    await plugin._clarify_questions(ClarifyRequest(goal="生成月报"))

    assert llm.calls[0]["endpoint"] == "selected-model"
    assert llm.calls[0]["policy"] == "require"
