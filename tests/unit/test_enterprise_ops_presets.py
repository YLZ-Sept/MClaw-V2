"""企业运营分类：8 个岗位预置的定义与部署。

覆盖：
1. 8 个岗位 id/名称/分类/i18n 完整，且与功能模块表一一对应；
2. 每个岗位都绑定知识库骨架，预算范围限于文档/Excel/知识库；
3. deploy_system_presets 在全新 ProfileStore 上创建全部岗位。
"""

from __future__ import annotations

import pytest

from mclaw.agents import presets as presets_mod
from mclaw.agents.presets import (
    _DIGITAL_EMPLOYEE_KB_BINDINGS,
    SYSTEM_PRESETS,
    deploy_system_presets,
    get_preset_by_id,
)
from mclaw.agents.profile import BUILTIN_CATEGORIES, AgentProfile, ProfileStore

ENTERPRISE_OPS = {
    "business-operations": "经营管理",
    "sales-management": "销售管理",
    "commerce-management": "商务管理",
    "finance-management": "财务管理",
    "hr-management": "人力资源",
    "project-management": "项目管理",
    "customer-management": "客户管理",
    "general-management": "综合管理",
}

# 目标功能模块表 -> 必须出现在岗位描述/岗位卡里的细项关键词
TARGET_ITEMS = {
    "business-operations": ["运营统筹", "决策分析", "可视化报表"],
    "sales-management": ["订单追踪", "品牌推广", "客户线索", "招投标"],
    "commerce-management": ["供应商", "合同", "订单跟踪", "库存"],
    "finance-management": ["应收应付", "工资核算", "数据分析"],
    "hr-management": ["招聘", "考勤", "考核"],
    "project-management": ["计划编制", "进度追踪", "周报", "项目报表"],
    "customer-management": ["售前", "售后", "知识库"],
    "general-management": ["日程", "公司制度", "社保"],
}

PROMPT_SECTIONS = (
    "【岗位职责】",
    "【输入】",
    "【输出】",
    "【协同分工】",
    "【异常处理】",
    "【必须转人工】",
    "【验收标准】",
)


def test_builtin_category_registered():
    categories = {c["id"]: c for c in BUILTIN_CATEGORIES}
    assert categories["enterprise-ops"]["label"] == "企业运营"
    assert categories["enterprise-ops"]["builtin"] is True


def test_enterprise_ops_presets_are_complete():
    for profile_id, name in ENTERPRISE_OPS.items():
        preset = get_preset_by_id(profile_id)
        assert preset is not None, profile_id
        assert preset.name == name
        assert preset.category == "enterprise-ops"
        assert preset.name_i18n.get("en")
        assert preset.description_i18n.get("en")
        assert preset.fallback_profile_id == "default"


def test_enterprise_ops_presets_bind_knowledge():
    for profile_id in ENTERPRISE_OPS:
        preset = get_preset_by_id(profile_id)
        assert preset.knowledge_collections, profile_id
        assert preset.knowledge_collections == _DIGITAL_EMPLOYEE_KB_BINDINGS[profile_id]


@pytest.mark.parametrize("profile_id", list(ENTERPRISE_OPS))
def test_prompts_stay_within_document_scope(profile_id):
    """用户约束：业务数据只做文档/Excel/知识库层面，不直接操作业务系统。"""
    prompt = get_preset_by_id(profile_id).custom_prompt
    assert any(
        marker in prompt for marker in ("不编造", "不虚构", "不直接操作", "不直接连接")
    ), profile_id
    assert any(
        marker in prompt for marker in ("文档", "Excel", "知识库", "台账")
    ), profile_id


@pytest.mark.parametrize("profile_id", list(TARGET_ITEMS))
def test_target_module_items_are_covered(profile_id):
    """8 个功能模块的细项必须逐条出现在岗位描述或岗位卡中。"""
    preset = get_preset_by_id(profile_id)
    text = f"{preset.description}\n{preset.custom_prompt}"
    missing = [item for item in TARGET_ITEMS[profile_id] if item not in text]
    assert not missing, f"{profile_id} 缺少目标细项: {missing}"


@pytest.mark.parametrize("profile_id", list(ENTERPRISE_OPS))
def test_prompt_has_uniform_job_card_sections(profile_id):
    prompt = get_preset_by_id(profile_id).custom_prompt
    missing = [section for section in PROMPT_SECTIONS if section not in prompt]
    assert not missing, f"{profile_id} 缺少岗位卡段落: {missing}"


@pytest.mark.parametrize("profile_id", list(ENTERPRISE_OPS))
def test_deliverables_and_example_prompts(profile_id):
    preset = get_preset_by_id(profile_id)
    assert len(preset.deliverables) == 3
    assert len(preset.example_prompts) == 3
    assert all(item.strip() for item in preset.deliverables + preset.example_prompts)


def test_preset_ids_unique():
    ids = [preset.id for preset in SYSTEM_PRESETS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("profile_id", ["finance-management", "hr-management"])
def test_sensitive_roles_minimal_permissions(profile_id):
    preset = get_preset_by_id(profile_id)
    assert preset.memory_mode == "isolated"
    assert preset.memory_inherit_global is False
    assert preset.tools_mode == "inclusive"
    assert preset.mcp_mode == "inclusive"
    assert preset.mcp_servers == []
    assert preset.plugins_mode == "inclusive"
    assert preset.plugins == []
    forbidden = {
        "browser",
        "desktop",
        "scheduled",
        "research",
        "run_shell",
        "run_powershell",
        "delete_file",
    }
    assert not forbidden & set(preset.tools)


@pytest.mark.parametrize(
    "profile_id",
    [
        "business-operations",
        "sales-management",
        "commerce-management",
        "project-management",
        "customer-management",
        "general-management",
    ],
)
def test_non_sensitive_roles_use_isolated_memory_and_whitelist(profile_id):
    preset = get_preset_by_id(profile_id)
    assert preset.memory_mode == "isolated"
    assert preset.memory_inherit_global is False
    assert "ask_user" in preset.tools
    assert preset.tools_mode == "inclusive"
    assert preset.mcp_mode == "inclusive"
    assert preset.mcp_servers == []
    assert preset.plugins_mode == "inclusive"
    assert preset.plugins == []
    forbidden = {
        "browser",
        "desktop",
        "scheduled",
        "research",
        "run_shell",
        "run_powershell",
        "delete_file",
    }
    assert not forbidden & set(preset.tools)


@pytest.mark.parametrize("profile_id", list(ENTERPRISE_OPS))
def test_all_enterprise_ops_roles_isolate_memory(profile_id):
    preset = get_preset_by_id(profile_id)
    assert preset.memory_mode == "isolated"
    assert preset.memory_inherit_global is False


@pytest.mark.parametrize("profile_id", list(ENTERPRISE_OPS))
def test_prompt_enforces_attachment_delivery(profile_id):
    prompt = get_preset_by_id(profile_id).custom_prompt
    assert "deliver_artifacts" in prompt
    assert "当前工作目录" in prompt


def test_job_card_contains_handoff_rules():
    prompt = get_preset_by_id("sales-management").custom_prompt
    for marker in ("线索跟进 → 销售管理", "合同事项 → 商务管理", "应收应付 → 财务管理", "售后问题 → 客户管理"):
        assert marker in prompt


def test_customer_and_project_use_dedicated_knowledge():
    assert get_preset_by_id("customer-management").knowledge_collections == ["sys-dept-customer"]
    assert get_preset_by_id("project-management").knowledge_collections == ["sys-dept-project"]


def test_deploy_backfills_prompt_and_job_card(tmp_path, monkeypatch):
    monkeypatch.setattr(presets_mod, "_ensure_digital_employee_kb_skeletons", lambda: None)
    store = ProfileStore(tmp_path)
    preset = get_preset_by_id("finance-management")
    old = preset.to_dict()
    old["custom_prompt"] = "旧提示词"
    old["deliverables"] = []
    old["example_prompts"] = []
    old["memory_mode"] = "shared"
    old["memory_isolation"] = "shared"
    old["tools"] = []
    old["tools_mode"] = "all"
    store.save(AgentProfile.from_dict(old))

    deploy_system_presets(store)

    stored = store.get("finance-management")
    assert stored.custom_prompt == preset.custom_prompt
    assert stored.deliverables == preset.deliverables
    assert stored.example_prompts == preset.example_prompts
    assert stored.memory_mode == "isolated"
    assert stored.tools_mode == "inclusive"


def test_deploy_creates_all_enterprise_ops_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(presets_mod, "_ensure_digital_employee_kb_skeletons", lambda: None)
    store = ProfileStore(tmp_path)

    deploy_system_presets(store)

    stored = {profile.id: profile for profile in store.list_all()}
    assert set(ENTERPRISE_OPS) <= set(stored)
    for profile_id in ENTERPRISE_OPS:
        assert stored[profile_id].category == "enterprise-ops"
        assert stored[profile_id].type.value == "system"
