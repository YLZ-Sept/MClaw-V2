"""企业运营岗位黄金评测集（8 岗位 × 5 类用例）。

评测集本身不调用 LLM，只保证：
1. 每个岗位都有正常、边界、信息不足、必须转人工、越权五类用例；
2. 交付物必须来自该岗位的标准交付物清单；
3. 用例带有可机器校验的期望（是否澄清/转人工/拒绝）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mclaw.agents.presets import get_preset_by_id

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "enterprise_ops_golden_cases.json"
ROLES = {
    "business-operations",
    "sales-management",
    "commerce-management",
    "finance-management",
    "hr-management",
    "project-management",
    "customer-management",
    "general-management",
}
CATEGORIES = {"normal", "boundary", "insufficient_info", "escalation", "out_of_scope"}


def _load_cases() -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 1
    return data["cases"]


def test_each_role_has_five_cases():
    cases = _load_cases()
    assert len(cases) == len(ROLES) * len(CATEGORIES) == 40
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    by_role: dict[str, set[str]] = {}
    for case in cases:
        by_role.setdefault(case["role"], set()).add(case["category"])

    assert set(by_role) == ROLES
    for role, categories in by_role.items():
        assert categories == CATEGORIES, role


@pytest.mark.parametrize("case", _load_cases(), ids=[c["id"] for c in _load_cases()])
def test_case_shape_and_expectations(case):
    assert case["role"] in ROLES
    assert case["input"].strip()
    expected = case["expected"]
    assert expected["must_include"]
    assert expected["note"].strip() or case["category"] == "normal"

    preset = get_preset_by_id(case["role"])
    assert preset is not None, case["role"]
    if expected["deliverable"]:
        assert expected["deliverable"] in preset.deliverables, (
            f"{case['id']} 的交付物不在 {case['role']} 的标准交付物中"
        )

    category = case["category"]
    if category == "normal":
        assert not expected["should_escalate"]
        assert not expected["should_refuse"]
        assert not expected["should_ask_clarify"]
    elif category == "boundary" or category == "insufficient_info":
        assert expected["should_ask_clarify"]
    elif category == "escalation":
        assert expected["should_escalate"]
    elif category == "out_of_scope":
        assert expected["should_refuse"]


def test_escalation_cases_are_sensitive_actions():
    """转人工用例必须涉及付款、签署、人事处分、对外承诺等不可逆动作。"""
    markers = ("付款", "核销", "盖章", "签署", "辞退", "罚款", "退款", "承诺", "预算", "延期")
    for case in _load_cases():
        if case["category"] != "escalation":
            continue
        assert any(marker in case["input"] for marker in markers), case["id"]


def test_out_of_scope_cases_ask_for_system_operation():
    markers = ("登录", "系统", "ERP", "CRM", "OA", "社保")
    for case in _load_cases():
        if case["category"] != "out_of_scope":
            continue
        assert any(marker in case["input"] for marker in markers), case["id"]
