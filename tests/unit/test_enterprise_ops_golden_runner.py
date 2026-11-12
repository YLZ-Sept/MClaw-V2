"""企业运营 L1 黄金集执行器：加载、校验、聚合与落盘。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mclaw.evaluation.golden_runner import (
    compare_runs,
    evaluate_response,
    list_runs,
    load_golden_cases,
    load_latest_run,
    run_golden_cases,
    save_run,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "enterprise_ops_golden_cases.json"


def _case(case_id: str):
    return next(case for case in load_golden_cases(FIXTURE) if case.id == case_id)


def test_load_all_golden_cases():
    cases = load_golden_cases(FIXTURE)
    assert len(cases) == 40
    assert {case.role for case in cases} == {
        "business-operations",
        "sales-management",
        "commerce-management",
        "finance-management",
        "hr-management",
        "project-management",
        "customer-management",
        "general-management",
    }
    assert {case.category for case in cases} == {
        "normal",
        "boundary",
        "insufficient_info",
        "escalation",
        "out_of_scope",
    }


def test_evaluate_normal_case():
    case = _case("business-operations-normal")
    checks = evaluate_response(case, "本月偏差主要来自回款延迟，数据来源是财务台账。")
    assert checks["passed"] is True
    assert checks["must_include_pass"] is True
    assert checks["behavior_pass"] is True


def test_evaluate_missing_must_include():
    case = _case("business-operations-normal")
    checks = evaluate_response(case, "本月经营情况整体正常。")
    assert checks["passed"] is False
    assert checks["must_include_pass"] is False


def test_evaluate_clarify_accepts_explicit_request_or_question():
    case = _case("business-operations-insufficient_info")
    checks = evaluate_response(case, "本月经营情况正常。")
    assert checks["clarify_pass"] is False
    assert checks["passed"] is False

    checks = evaluate_response(case, "需要您补充统计期间和指标范围，请提供相关数据？")
    assert checks["clarify_pass"] is True

    # 表格/清单型的澄清回答不一定带问号，但“待确认/请提供”同样算澄清。
    checks = evaluate_response(case, "请提供统计期间、指标口径和部门范围，数据待确认。")
    assert checks["clarify_pass"] is True

    # 同义表达：模型常写“需要提供”，不应因为没写“请提供”而判失败。
    checks = evaluate_response(case, "需要提供统计期间和指标口径，以及部门范围。")
    assert checks["must_include_pass"] is True
    assert checks["passed"] is True


def test_evaluate_escalation_and_refusal():
    escalation = _case("business-operations-escalation")
    assert evaluate_response(escalation, "预算调整需要管理层审批和人工确认。")["passed"] is True
    assert evaluate_response(escalation, "预算可以调整。")["escalate_pass"] is False

    refusal = _case("business-operations-out_of_scope")
    assert evaluate_response(
        refusal, "我不直接连接 ERP，也无法提供账号密码导出。"
    )["passed"] is True
    assert evaluate_response(refusal, "可以帮您导出 ERP 明细。")["refuse_pass"] is False


def test_evaluate_deliverable_hit():
    case = _case("sales-management-normal")
    assert case.deliverable
    hit = evaluate_response(case, f"以下是{case.deliverable}，包含线索和跟进人。")
    assert hit["deliverable_hit"] is True


class _FakeUsage:
    input_tokens = 10
    output_tokens = 20


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.model = "fake-model"
        self.endpoint_name = "fake-endpoint"
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    async def chat(self, messages, system="", **kwargs):
        prompt = str(messages[0].content)
        for needle, text in self._responses.items():
            if needle in prompt:
                return _FakeResponse(text)
        return _FakeResponse("没有命中测试响应")


async def test_run_golden_cases_aggregates(tmp_path):
    case_ids = [
        "business-operations-normal",
        "business-operations-insufficient_info",
        "business-operations-escalation",
        "business-operations-out_of_scope",
    ]
    cases = [_case(case_id) for case_id in case_ids]
    client = _FakeClient(
        {
            "10 月": "本月偏差主要来自回款延迟，数据来源是财务台账。",
            "公司最近经营怎么样": "需要您补充统计期间和指标范围，请提供相关数据？",
            "直接批准": "预算调整需要管理层审批和人工确认。",
            "ERP": "我不直接连接 ERP 系统，也无法提供账号密码导出。",
        }
    )

    result = await run_golden_cases(cases, client=client, km=None, use_kb=False)

    assert result.total == 4
    assert result.passed == 4
    assert result.pass_rate == 1.0
    assert result.by_role["business-operations"]["passed"] == 4
    assert result.by_category["normal"]["passed"] == 1
    assert result.model == "fake-model"
    assert result.endpoint == "fake-endpoint"

    path = save_run(result, tmp_path)
    assert path.is_file()
    latest = load_latest_run(tmp_path)
    assert latest is not None
    assert latest["total"] == 4
    assert len(latest["cases"]) == 4
    # latest.json 与 run_*.json 内容一致
    assert json.loads(path.read_text(encoding="utf-8"))["pass_rate"] == 1.0

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["total"] == 4
    assert runs[0]["pass_rate"] == 1.0


def test_compare_runs_detects_overall_and_role_drop():
    baseline = {
        "started_at": "2026-11-12T00:00:00Z",
        "pass_rate": 0.95,
        "by_role": {
            "finance-management": {"pass_rate": 1.0},
            "sales-management": {"pass_rate": 1.0},
        },
    }
    current = {
        "pass_rate": 0.85,
        "by_role": {
            "finance-management": {"pass_rate": 0.8},
            "sales-management": {"pass_rate": 1.0},
        },
    }
    comparison = compare_runs(baseline, current, max_drop=0.05)
    assert comparison["dropped"] is True
    assert comparison["delta"] == -0.1
    assert comparison["role_drops"] == {"finance-management": -0.2}


def test_compare_runs_without_baseline_does_not_drop():
    comparison = compare_runs(None, {"pass_rate": 0.9})
    assert comparison["dropped"] is False
    assert comparison["delta"] is None
    assert comparison["role_drops"] == {}
