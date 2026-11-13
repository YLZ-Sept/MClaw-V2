"""技能执行期白名单：INCLUSIVE 不只是目录隐藏，还要拦截执行。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mclaw.agents.presets import get_preset_by_id
from mclaw.agents.profile import AgentProfile, SkillsMode
from mclaw.core.agent_context import reset_current_agent_context, set_current_agent_context
from mclaw.core.skill_permissions import (
    current_profile_for_agent,
    filter_skill_entries,
    is_skill_allowed,
)
from mclaw.tools.handlers.skills import SkillsHandler


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = {entry.skill_id: entry for entry in entries}

    def get(self, skill_name: str):
        return self._entries.get(skill_name)

    def list_all(self, include_disabled: bool = False):
        return list(self._entries.values())


class _FakeLoader:
    def __init__(self):
        self.script_calls: list[tuple[str, str]] = []
        self.reference_calls: list[tuple[str, str]] = []

    def run_script(self, skill_name: str, script_name: str, *args, **kwargs):
        self.script_calls.append((skill_name, script_name))
        return {"ok": True}

    def get_reference(self, skill_name: str, ref_name: str):
        self.reference_calls.append((skill_name, ref_name))
        return "reference body"


def _entry(skill_id: str, *, system: bool = False, disabled: bool = False):
    return SimpleNamespace(
        skill_id=skill_id,
        name=skill_id,
        description="",
        when_to_use="",
        name_i18n={},
        system=system,
        disabled=disabled,
        catalog_hidden=False,
        disable_model_invocation=False,
        skill_path="",
        tool_name=None,
        handler=None,
        category="general",
        allowed_tools=[],
        python_dependencies=[],
        python_env="",
        get_body=lambda: "skill body",
    )


def _agent(profile, entries):
    loader = _FakeLoader()
    agent = SimpleNamespace(
        _agent_profile=profile,
        skill_registry=_FakeRegistry(entries),
        skill_loader=loader,
        _skill_usage_tracker=None,
        _execution_env_spec=None,
    )
    return agent, loader


def _finance_profile() -> AgentProfile:
    preset = get_preset_by_id("finance-management")
    assert preset is not None
    return AgentProfile.from_dict(preset.to_dict())


def test_inclusive_allows_listed_and_essential_only():
    profile = _finance_profile()
    assert is_skill_allowed(profile, "docx") is True
    assert is_skill_allowed(profile, "bid-writer") is False
    assert is_skill_allowed(profile, "list-skills") is True


def test_all_mode_allows_everything_and_missing_profile_fails_open():
    profile = AgentProfile(id="open", name="Open", skills=[], skills_mode=SkillsMode.ALL)
    assert is_skill_allowed(profile, "bid-writer") is True
    assert is_skill_allowed(None, "bid-writer") is True


def test_exclusive_blocks_listed_but_keeps_essentials():
    profile = AgentProfile(
        id="excl", name="Excl", skills=["bid-writer"], skills_mode=SkillsMode.EXCLUSIVE
    )
    assert is_skill_allowed(profile, "bid-writer") is False
    assert is_skill_allowed(profile, "docx") is True
    assert is_skill_allowed(profile, "list-skills") is True


def test_name_normalization_handles_underscores_and_namespaces():
    profile = AgentProfile(
        id="norm",
        name="Norm",
        skills=["Doc_Coauthoring", "plugin@docx"],
        skills_mode=SkillsMode.INCLUSIVE,
    )
    assert is_skill_allowed(profile, "doc-coauthoring") is True
    assert is_skill_allowed(profile, "docx") is True
    assert is_skill_allowed(profile, "xlsx") is False


def test_list_skills_filters_blocked_entries():
    profile = _finance_profile()
    handler = SkillsHandler(_agent(profile, [])[0])
    entries = [_entry("docx"), _entry("bid-writer"), _entry("list-skills", system=True)]

    names = [entry.skill_id for entry in filter_skill_entries(entries, profile)]

    assert names == ["docx", "list-skills"]
    assert handler._skill_guard("docx") is None
    assert handler._skill_guard("bid-writer") is not None


def test_run_skill_script_blocks_before_loader():
    profile = _finance_profile()
    agent, loader = _agent(profile, [_entry("bid-writer")])
    handler = SkillsHandler(agent)

    result = handler._run_skill_script({"skill_name": "bid-writer", "script_name": "x.py"})

    assert "未对该岗位启用" in result
    assert loader.script_calls == []


def test_get_skill_info_blocks_listed_out_skill():
    profile = _finance_profile()
    agent, _ = _agent(profile, [_entry("bid-writer")])
    handler = SkillsHandler(agent)

    result = handler._get_skill_info({"skill_name": "bid-writer"})

    assert "未对该岗位启用" in result


def test_get_skill_reference_blocks_before_loader():
    profile = _finance_profile()
    agent, loader = _agent(profile, [_entry("bid-writer")])
    handler = SkillsHandler(agent)

    result = handler._get_skill_reference({"skill_name": "bid-writer"})

    assert "未对该岗位启用" in result
    assert loader.reference_calls == []


@pytest.mark.asyncio
async def test_execute_skill_blocks_before_reasoning():
    profile = _finance_profile()
    agent, _ = _agent(profile, [_entry("bid-writer")])
    handler = SkillsHandler(agent)

    result = await handler._execute_skill({"skill_name": "bid-writer", "task": "写标书"})

    assert "未对该岗位启用" in result


def test_org_node_profile_falls_back_to_context(monkeypatch):
    profile = _finance_profile()
    store = SimpleNamespace(get=lambda profile_id: profile if profile_id == "finance-management" else None)
    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: store)
    agent = SimpleNamespace()

    tokens = set_current_agent_context(profile_id="finance-management")
    try:
        assert current_profile_for_agent(agent) is profile
        handler = SkillsHandler(agent)
        assert handler._skill_guard("bid-writer") is not None
        assert handler._skill_guard("docx") is None
    finally:
        reset_current_agent_context(tokens)
