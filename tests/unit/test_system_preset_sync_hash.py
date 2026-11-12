"""系统预置的版本标记与“有更新”判断。

覆盖：
1. 新部署的预置会写入当前哈希；
2. 未自定义的预置在受管字段变化时自动同步并刷新哈希；
3. 已自定义的预置不被覆盖，旧版无哈希时按受管字段差异判断是否提示；
4. 自定义预置能识别真实预设更新，且不受非受管字段影响；
5. 无序列表（tools/skills/knowledge）顺序变化不触发更新，岗位卡等有序字段变化会触发。
"""

from __future__ import annotations

import pytest

from mclaw.agents import presets as presets_mod
from mclaw.agents.presets import (
    compute_preset_hash,
    deploy_system_presets,
    get_preset_by_id,
    preset_apply_updates,
    preset_diff,
    preset_ignore_update,
    preset_update_available,
)
from mclaw.agents.profile import AgentProfile, ProfileStore

PRESET_ID = "finance-management"


def _store(tmp_path, monkeypatch) -> ProfileStore:
    monkeypatch.setattr(presets_mod, "_ensure_digital_employee_kb_skeletons", lambda: None)
    return ProfileStore(tmp_path)


def _preset_copy(**overrides) -> AgentProfile:
    data = get_preset_by_id(PRESET_ID).to_dict()
    data.update(overrides)
    return AgentProfile.from_dict(data)


def test_deploy_writes_current_hash(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)

    deploy_system_presets(store)

    preset = get_preset_by_id(PRESET_ID)
    stored = store.get(PRESET_ID)
    assert stored is not None
    assert stored.preset_synced_hash
    assert stored.preset_synced_hash == compute_preset_hash(preset)


def test_uncustomized_preset_syncs_and_refreshes_hash(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    stale = _preset_copy(
        custom_prompt="旧提示词",
        deliverables=[],
        example_prompts=[],
        tools=[],
        tools_mode="all",
        mcp_servers=["old-server"],
        plugins=["old-plugin"],
        preset_synced_hash="",
    )
    store.save(stale)

    deploy_system_presets(store)

    preset = get_preset_by_id(PRESET_ID)
    stored = store.get(PRESET_ID)
    assert stored.custom_prompt == preset.custom_prompt
    assert stored.deliverables == preset.deliverables
    assert stored.example_prompts == preset.example_prompts
    assert stored.tools == preset.tools
    assert stored.tools_mode == preset.tools_mode
    assert stored.mcp_servers == preset.mcp_servers
    assert stored.plugins == preset.plugins
    assert stored.preset_synced_hash == compute_preset_hash(preset)


def test_legacy_uncustomized_preset_with_equal_fields_only_gets_hash(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    # 先完成一次全量部署，确保其他预置不会计入第二次部署的升级数。
    deploy_system_presets(store)
    legacy = AgentProfile.from_dict(
        {**store.get(PRESET_ID).to_dict(), "preset_synced_hash": ""}
    )
    store.save(legacy)

    deployed = deploy_system_presets(store)

    stored = store.get(PRESET_ID)
    assert stored.preset_synced_hash == compute_preset_hash(get_preset_by_id(PRESET_ID))
    # 字段本来就一致，只补哈希，不应计为升级。
    assert deployed == 0


def test_legacy_customized_preset_is_preserved_and_flags_real_differences(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    customized = _preset_copy(
        custom_prompt="用户自己的提示词",
        user_customized=True,
        preset_synced_hash="",
    )
    store.save(customized)

    deploy_system_presets(store)

    stored = store.get(PRESET_ID)
    assert stored.custom_prompt == "用户自己的提示词"
    assert stored.user_customized is True
    assert stored.preset_synced_hash == ""
    # 岗位卡确实不同，应提示更新；不能用当前哈希把差异一次性抹平。
    assert preset_update_available(stored) is True
    assert "custom_prompt" in preset_diff(stored)


def test_legacy_customized_preset_with_only_identity_change_is_not_flagged(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    customized = _preset_copy(
        name="改个名字",
        icon="🧪",
        color="#000000",
        user_customized=True,
        preset_synced_hash="",
    )
    store.save(customized)

    deploy_system_presets(store)

    stored = store.get(PRESET_ID)
    assert stored.preset_synced_hash == ""
    # name/icon/color 不是受管字段，不应误报预设更新。
    assert preset_update_available(stored) is False


def test_customized_preset_detects_real_preset_update(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    baseline = compute_preset_hash(get_preset_by_id(PRESET_ID))
    customized = _preset_copy(
        custom_prompt="用户自己的提示词",
        user_customized=True,
        preset_synced_hash=baseline,
    )
    store.save(customized)
    stored = store.get(PRESET_ID)
    assert preset_update_available(stored) is False

    original = get_preset_by_id(PRESET_ID)
    changed = AgentProfile.from_dict(
        {**original.to_dict(), "custom_prompt": original.custom_prompt + "\n新增一条要求"}
    )
    monkeypatch.setattr(
        presets_mod,
        "SYSTEM_PRESETS",
        [changed if p.id == PRESET_ID else p for p in presets_mod.SYSTEM_PRESETS],
    )

    assert preset_update_available(stored) is True


def test_hash_ignores_non_tracked_fields():
    preset = get_preset_by_id(PRESET_ID)
    changed = AgentProfile.from_dict(
        {
            **preset.to_dict(),
            "name": "改个名字",
            "icon": "🧪",
            "color": "#000000",
            "preferred_endpoint": "another-endpoint",
        }
    )
    assert compute_preset_hash(changed) == compute_preset_hash(preset)


def test_hash_sorts_unordered_fields_but_keeps_deliverable_order():
    preset = get_preset_by_id(PRESET_ID)
    reordered_tools = AgentProfile.from_dict(
        {**preset.to_dict(), "tools": list(reversed(preset.tools))}
    )
    assert compute_preset_hash(reordered_tools) == compute_preset_hash(preset)

    reordered_deliverables = AgentProfile.from_dict(
        {**preset.to_dict(), "deliverables": list(reversed(preset.deliverables))}
    )
    assert compute_preset_hash(reordered_deliverables) != compute_preset_hash(preset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("custom_prompt", "新提示词"),
        ("description", "新描述"),
        ("deliverables", ["新交付物"]),
        ("example_prompts", ["新示例"]),
        ("knowledge_collections", ["sys-dept-other"]),
        ("memory_mode", "shared"),
        ("memory_inherit_global", True),
        ("category", "other-category"),
    ],
)
def test_hash_tracks_managed_fields(field, value):
    preset = get_preset_by_id(PRESET_ID)
    overrides = {**preset.to_dict(), field: value}
    if field == "memory_mode":
        # from_dict 中 memory_isolation 是新 canonical 名，会覆盖 memory_mode。
        overrides["memory_isolation"] = value
    changed = AgentProfile.from_dict(overrides)
    assert compute_preset_hash(changed) != compute_preset_hash(preset)


def test_diff_ignore_and_apply_flow(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    deploy_system_presets(store)
    original = get_preset_by_id(PRESET_ID)

    # 用户改提示词后 user_customized=True；此时预设未变，不应提示更新。
    store.update(PRESET_ID, {"custom_prompt": "用户自己的提示词"})
    stored = store.get(PRESET_ID)
    assert stored.user_customized is True
    assert preset_update_available(stored) is False

    # 模拟预置本身发生变化。
    changed = AgentProfile.from_dict(
        {**original.to_dict(), "custom_prompt": original.custom_prompt + "\n新增一条要求"}
    )
    monkeypatch.setattr(
        presets_mod,
        "SYSTEM_PRESETS",
        [changed if p.id == PRESET_ID else p for p in presets_mod.SYSTEM_PRESETS],
    )
    stored = store.get(PRESET_ID)
    assert preset_update_available(stored) is True
    diff = preset_diff(stored)
    assert "custom_prompt" in diff
    assert diff["custom_prompt"]["preset"] == changed.custom_prompt

    # 忽略后不再提示；应用后回到已同步状态，且清除忽略标记。
    ignored = store.update(PRESET_ID, preset_ignore_update(stored))
    assert preset_update_available(ignored) is False

    applied = store.update(PRESET_ID, preset_apply_updates(ignored))
    assert applied.custom_prompt == changed.custom_prompt
    assert applied.preset_ignored_hash == ""
    assert preset_update_available(applied) is False


def test_null_preset_hashes_normalize_to_empty():
    data = get_preset_by_id(PRESET_ID).to_dict()
    data["preset_synced_hash"] = None
    data["preset_ignored_hash"] = None
    profile = AgentProfile.from_dict(data)
    assert profile.preset_synced_hash == ""
    assert profile.preset_ignored_hash == ""
