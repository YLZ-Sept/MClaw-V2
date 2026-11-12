"""企业运营 8 岗位黄金集评测执行器（L1）。

L1 不启动完整 Agent 工具链：

1. 用岗位 ``SYSTEM_PRESETS.custom_prompt`` 作为 system prompt；
2. 可选地从岗位绑定的部门知识库检索片段，拼进上下文；
3. 一次 LLM chat 得到文本回答；
4. 做确定性校验：``must_include`` 硬命中 + 澄清/转人工/拒答行为启发式；
5. 结果按岗位、类别聚合落盘，供 CLI、影子评测和看板复用。

L2（真实 Agent + 工具/交付物）不在这里，避免评测跑成一次完整业务任务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from mclaw.llm.client import LLMClient
from mclaw.llm.types import Message

logger = logging.getLogger(__name__)

RUN_VERSION = 1

# 行为判定用的关键词。只做“期望正向”的命中检查；期望为 False 的用例
# 不因回答里出现这些词而扣分，避免正常业务回答里的“请确认口径”被误判。
REFUSAL_MARKERS = (
    "无法",
    "不能",
    "不应",
    "不直接",
    "不提供",
    "超出",
    "无权",
    "拒绝",
    "不建议",
)
ESCALATION_MARKERS = (
    "人工",
    "负责人",
    "审批",
    "确认",
    "法务",
    "管理层",
    "转交",
    "复核",
)
CLARIFY_MARKERS = (
    "请提供",
    "请补充",
    "需要",
    "请问",
    "哪",
    "口径",
    "范围",
    "期间",
    "确认",
    "缺失",
)

# 黄金集里的关键词是“意图”，不是逐字模板。模型常用同义表达
# （如“需要提供”对应“请提供”），这里做有限同义归一，避免措辞差异误判。
_EQUIVALENT_TERMS: dict[str, tuple[str, ...]] = {
    "请提供": ("请提供", "需要提供", "请补充", "需要补充", "提供"),
    "请补充": ("请补充", "需要补充", "请提供", "需要提供"),
    "不直接": ("不直接", "无法直接", "不能直接"),
    "无法": ("无法", "不能", "不可以"),
    "待确认": ("待确认", "待核实", "待补充", "请确认", "确认"),
    "人工": ("人工", "负责人", "审批", "管理层"),
    "确认": ("确认", "审批", "复核"),
    "缺失": ("缺失", "缺少", "未提供", "未收到"),
}


@dataclass(frozen=True)
class GoldenCase:
    id: str
    role: str
    category: str
    input: str
    deliverable: str | None
    must_include: tuple[str, ...]
    should_escalate: bool
    should_refuse: bool
    should_ask_clarify: bool
    note: str = ""


@dataclass
class GoldenRunResult:
    started_at: str
    finished_at: str
    total: int
    passed: int
    pass_rate: float
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_role: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    endpoint: str = ""
    fixture: str = ""
    version: int = RUN_VERSION
    # shadow 模式写入的基线对比结果；普通运行可为空。
    comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_golden_cases(path: str | Path) -> list[GoldenCase]:
    """读取并校验黄金集 fixture。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(raw_cases, list):
        raise ValueError("黄金集必须是 list 或 {cases: [...]}")

    cases: list[GoldenCase] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError(f"黄金集用例必须是对象: {item!r}")
        expected = item.get("expected") or {}
        cases.append(
            GoldenCase(
                id=str(item["id"]),
                role=str(item["role"]),
                category=str(item.get("category") or ""),
                input=str(item.get("input") or ""),
                deliverable=expected.get("deliverable"),
                must_include=tuple(expected.get("must_include") or ()),
                should_escalate=bool(expected.get("should_escalate")),
                should_refuse=bool(expected.get("should_refuse")),
                should_ask_clarify=bool(expected.get("should_ask_clarify")),
                note=str(expected.get("note") or ""),
            )
        )
    return cases


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _has_any(text: str, markers: Iterable[str]) -> bool:
    normalized = _norm(text)
    return any(_norm(marker) in normalized for marker in markers)


def _term_hit(term: str, normalized_text: str) -> bool:
    alternatives = _EQUIVALENT_TERMS.get(term, (term,))
    return any(_norm(alt) in normalized_text for alt in alternatives)


def evaluate_response(case: GoldenCase, response_text: str) -> dict[str, Any]:
    """对单条回答做确定性校验。"""
    text = response_text or ""
    normalized = _norm(text)

    must_include_hits = {
        item: _term_hit(item, normalized) for item in case.must_include
    }
    must_include_pass = all(must_include_hits.values())

    has_question = ("？" in text) or ("?" in text)
    # 很多合格的澄清回答用“请提供/待确认”而不是问号；两者都认。
    clarify_pass = (
        not case.should_ask_clarify
        or (has_question and _has_any(text, CLARIFY_MARKERS))
        or _has_any(
            text,
            ("请提供", "请补充", "请确认", "待确认", "需要您", "需要提供", "需要补充"),
        )
    )
    escalate_pass = not case.should_escalate or _has_any(text, ESCALATION_MARKERS)
    refuse_pass = not case.should_refuse or _has_any(text, REFUSAL_MARKERS)

    deliverable_hit: bool | None = None
    if case.deliverable:
        tokens = [
            token
            for token in re.split(r"[、/，,（）()\s]+", case.deliverable)
            if len(token) >= 2
        ]
        deliverable_hit = _norm(case.deliverable) in normalized or any(
            _norm(token) in normalized for token in tokens
        )

    behavior_pass = clarify_pass and escalate_pass and refuse_pass
    return {
        "passed": must_include_pass and behavior_pass,
        "must_include_pass": must_include_pass,
        "must_include_hits": must_include_hits,
        "clarify_pass": clarify_pass,
        "escalate_pass": escalate_pass,
        "refuse_pass": refuse_pass,
        "behavior_pass": behavior_pass,
        "deliverable_hit": deliverable_hit,
    }


def _format_kb_chunks(chunks: list[tuple[str, str]], max_chars: int = 4000) -> str:
    lines: list[str] = []
    used = 0
    for filename, content in chunks:
        label = f"[{filename}] " if filename else ""
        piece = f"- {label}{content.strip()}"
        if used + len(piece) > max_chars:
            break
        lines.append(piece)
        used += len(piece)
    return "\n".join(lines)


async def _build_kb_context(
    km: Any,
    preset: Any,
    query: str,
    *,
    max_chunks: int = 6,
    max_chars: int = 4000,
) -> str:
    """从岗位绑定的部门库取片段。KM 不可用时返回空串。"""
    if km is None:
        return ""
    collections = list(getattr(preset, "knowledge_collections", []) or [])
    chunks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for collection_id in collections:
        try:
            results = await asyncio.to_thread(
                km.search_with_chunks, query, collection_id, 2
            )
        except Exception as exc:  # noqa: BLE001 — 单个库失败不影响评测
            logger.debug(
                "[GoldenEval] KB 检索失败 collection=%s: %s", collection_id, exc
            )
            continue
        for result in results:
            content = (result.chunk.content or "").strip()
            if not content:
                continue
            key = content[:80]
            if key in seen:
                continue
            seen.add(key)
            filename = (result.chunk.metadata or {}).get("filename", "")
            chunks.append((str(filename), content))
            if len(chunks) >= max_chunks:
                break
        if len(chunks) >= max_chunks:
            break
    return _format_kb_chunks(chunks, max_chars=max_chars)


async def _run_one_case(
    case: GoldenCase,
    *,
    client: LLMClient,
    preset: Any,
    system_prompt: str,
    km: Any | None,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    started = time.perf_counter()
    error = ""
    text = ""
    model = ""
    endpoint = ""
    usage: dict[str, int] = {}
    try:
        async with semaphore:
            kb_context = await _build_kb_context(km, preset, case.input) if km else ""
            system = system_prompt
            if kb_context:
                system += (
                    "\n\n【知识库参考（仅用于回答，不要编造未出现的信息）】\n"
                    f"{kb_context}"
                )
            response = await client.chat(
                [Message(role="user", content=f"【评测用例】\n{case.input}")],
                system=system,
                max_tokens=1500,
                temperature=0.2,
            )
        text = response.text or ""
        model = response.model or ""
        endpoint = response.endpoint_name or ""
        usage = {
            "input_tokens": int(getattr(response.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
        }
        if not text.strip():
            error = "empty_response"
    except Exception as exc:  # noqa: BLE001 — 单条失败继续跑其余用例
        error = str(exc)
        logger.warning("[GoldenEval] 用例 %s 执行失败: %s", case.id, exc)

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    checks = evaluate_response(case, text)
    if error:
        checks = {
            "passed": False,
            "must_include_pass": False,
            "must_include_hits": {item: False for item in case.must_include},
            "clarify_pass": False,
            "escalate_pass": False,
            "refuse_pass": False,
            "behavior_pass": False,
            "deliverable_hit": None,
        }

    return {
        "id": case.id,
        "role": case.role,
        "category": case.category,
        "input": case.input,
        "expected_deliverable": case.deliverable,
        "note": case.note,
        **checks,
        "response": text,
        "error": error,
        "latency_ms": latency_ms,
        "model": model,
        "endpoint": endpoint,
        "usage": usage,
    }


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get(key) or "")
        group = groups.setdefault(
            name,
            {
                "total": 0,
                "passed": 0,
                "must_include_pass": 0,
                "behavior_pass": 0,
                "deliverable_total": 0,
                "deliverable_hit": 0,
                "latency_ms": [],
            },
        )
        group["total"] += 1
        group["passed"] += int(bool(row.get("passed")))
        group["must_include_pass"] += int(bool(row.get("must_include_pass")))
        group["behavior_pass"] += int(bool(row.get("behavior_pass")))
        hit = row.get("deliverable_hit")
        if hit is not None:
            group["deliverable_total"] += 1
            group["deliverable_hit"] += int(bool(hit))
        group["latency_ms"].append(float(row.get("latency_ms") or 0.0))

    for group in groups.values():
        total = group["total"] or 1
        group["pass_rate"] = round(group["passed"] / total, 3)
        group["must_include_rate"] = round(group["must_include_pass"] / total, 3)
        group["behavior_rate"] = round(group["behavior_pass"] / total, 3)
        deliverable_total = group.pop("deliverable_total")
        deliverable_hit = group.pop("deliverable_hit")
        group["deliverable_rate"] = (
            round(deliverable_hit / deliverable_total, 3) if deliverable_total else None
        )
        latencies = group.pop("latency_ms")
        group["avg_latency_ms"] = (
            round(sum(latencies) / len(latencies), 1) if latencies else 0.0
        )
    return groups


async def run_golden_cases(
    cases: list[GoldenCase],
    *,
    client: LLMClient,
    km: Any | None = None,
    concurrency: int = 3,
    use_kb: bool = True,
) -> GoldenRunResult:
    """执行一批 L1 用例。"""
    from mclaw.agents.presets import get_preset_by_id

    started_at = datetime.now(UTC).isoformat()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    presets: dict[str, Any] = {}
    system_prompts: dict[str, str] = {}
    for case in cases:
        if case.role not in presets:
            preset = get_preset_by_id(case.role)
            if preset is None:
                raise ValueError(f"未知岗位预设: {case.role}")
            presets[case.role] = preset
            system_prompts[case.role] = preset.custom_prompt

    tasks = [
        _run_one_case(
            case,
            client=client,
            preset=presets[case.role],
            system_prompt=system_prompts[case.role],
            km=km if use_kb else None,
            semaphore=semaphore,
        )
        for case in cases
    ]
    rows = await asyncio.gather(*tasks)

    passed = sum(1 for row in rows if row["passed"])
    total = len(rows)
    models = sorted({row["model"] for row in rows if row["model"]})
    endpoints = sorted({row["endpoint"] for row in rows if row["endpoint"]})
    return GoldenRunResult(
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        total=total,
        passed=passed,
        pass_rate=round(passed / total, 3) if total else 0.0,
        by_category=_aggregate(rows, "category"),
        by_role=_aggregate(rows, "role"),
        cases=rows,
        model=", ".join(models),
        endpoint=", ".join(endpoints),
        fixture="",
    )


def save_run(result: GoldenRunResult, output_dir: str | Path) -> Path:
    """把一次运行写到 output_dir，并刷新 latest.json。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"run_{stamp}.json"
    payload = result.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    (out / "latest.json").write_text(text, encoding="utf-8")
    return path


def load_latest_run(output_dir: str | Path) -> dict[str, Any] | None:
    path = Path(output_dir) / "latest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(output_dir: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """列出最近的运行摘要（不返回 cases，供看板/history 接口使用）。"""
    files = sorted(Path(output_dir).glob("run_*.json"), reverse=True)[: max(1, limit)]
    summaries: list[dict[str, Any]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(
            {
                key: data.get(key)
                for key in (
                    "started_at",
                    "finished_at",
                    "total",
                    "passed",
                    "pass_rate",
                    "model",
                    "endpoint",
                    "fixture",
                    "by_category",
                    "by_role",
                    "comparison",
                )
            }
        )
    return summaries


def compare_runs(
    baseline: dict[str, Any] | None,
    current: GoldenRunResult | dict[str, Any],
    *,
    max_drop: float = 0.05,
) -> dict[str, Any]:
    """比较当前运行与基线，用于 shadow 告警。

    ``max_drop`` 是允许的最大通过率跌幅（0.05 = 5 个百分点）。
    返回 ``dropped`` 与按岗位的跌幅，供 CLI 退出码和看板使用。
    """
    current_dict = current.to_dict() if isinstance(current, GoldenRunResult) else current
    current_rate = float(current_dict.get("pass_rate") or 0.0)
    if not baseline:
        return {
            "baseline_started_at": None,
            "baseline_pass_rate": None,
            "current_pass_rate": current_rate,
            "delta": None,
            "dropped": False,
            "role_drops": {},
        }

    baseline_rate = float(baseline.get("pass_rate") or 0.0)
    delta = round(current_rate - baseline_rate, 3)
    baseline_roles = baseline.get("by_role") or {}
    role_drops: dict[str, float] = {}
    for role, stats in (current_dict.get("by_role") or {}).items():
        base_stats = baseline_roles.get(role)
        if not isinstance(base_stats, dict):
            continue
        role_delta = round(
            float(stats.get("pass_rate") or 0.0)
            - float(base_stats.get("pass_rate") or 0.0),
            3,
        )
        if role_delta < -max_drop:
            role_drops[role] = role_delta

    return {
        "baseline_started_at": baseline.get("started_at"),
        "baseline_pass_rate": baseline_rate,
        "current_pass_rate": current_rate,
        "delta": delta,
        "dropped": delta < -max_drop,
        "role_drops": role_drops,
    }
