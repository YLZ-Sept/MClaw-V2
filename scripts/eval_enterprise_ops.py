#!/usr/bin/env python
"""企业运营 8 岗位 L1 黄金集评测。

用法::

    python scripts/eval_enterprise_ops.py --dry-run
    python scripts/eval_enterprise_ops.py --roles finance-management,hr-management
    python scripts/eval_enterprise_ops.py --no-kb --concurrency 2
    python scripts/eval_enterprise_ops.py --fail-under 0.9

结果写到 ``<data>/evals/enterprise_ops/run_*.json`` 并刷新 ``latest.json``；
默认使用 FTS5 知识库（不加载向量模型），避免评测脚本与在线服务抢显存。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _split(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _print_table(result) -> None:
    print(
        f"\n总计: {result.passed}/{result.total} 通过，"
        f"通过率 {result.pass_rate:.1%}，模型 {result.model or '-'}，"
        f"端点 {result.endpoint or '-'}"
    )
    print("\n按岗位:")
    print(f"{'岗位':<28}{'通过':>8}{'通过率':>10}{'必含':>8}{'行为':>8}{'交付物':>8}{'延迟ms':>10}")
    for role, stats in sorted(result.by_role.items()):
        deliverable = (
            f"{stats['deliverable_rate']:.0%}" if stats["deliverable_rate"] is not None else "-"
        )
        print(
            f"{role:<28}{stats['passed']:>4}/{stats['total']:<3}"
            f"{stats['pass_rate']:>9.0%}{stats['must_include_rate']:>8.0%}"
            f"{stats['behavior_rate']:>8.0%}{deliverable:>8}"
            f"{stats['avg_latency_ms']:>10.0f}"
        )
    print("\n按类别:")
    print(f"{'类别':<20}{'通过':>8}{'通过率':>10}{'必含':>8}{'行为':>8}{'交付物':>8}")
    for category, stats in sorted(result.by_category.items()):
        deliverable = (
            f"{stats['deliverable_rate']:.0%}" if stats["deliverable_rate"] is not None else "-"
        )
        print(
            f"{category:<20}{stats['passed']:>4}/{stats['total']:<3}"
            f"{stats['pass_rate']:>9.0%}{stats['must_include_rate']:>8.0%}"
            f"{stats['behavior_rate']:>8.0%}{deliverable:>8}"
        )
    errors = [case for case in result.cases if case.get("error")]
    if errors:
        print(f"\n执行失败 {len(errors)} 条:")
        for case in errors[:10]:
            print(f"  - {case['id']}: {case['error']}")


async def _run(args) -> int:
    from mclaw.config import settings
    from mclaw.evaluation.golden_runner import (
        compare_runs,
        load_golden_cases,
        load_latest_run,
        run_golden_cases,
        save_run,
    )
    from mclaw.llm.client import LLMClient

    project_root = Path(getattr(settings, "project_root", None) or Path.cwd())
    fixture = args.fixture or (project_root / "tests" / "fixtures" / "enterprise_ops_golden_cases.json")
    data_root = Path(getattr(settings, "data_dir", None) or (project_root / "data"))
    kb_dir = args.data_dir or (data_root / "knowledge")
    output_dir = args.output_dir or (data_root / "evals" / "enterprise_ops")

    cases = load_golden_cases(fixture)
    roles = _split(args.roles)
    categories = _split(args.categories)
    ids = _split(args.ids)
    if roles:
        cases = [case for case in cases if case.role in roles]
    if categories:
        cases = [case for case in cases if case.category in categories]
    if ids:
        cases = [case for case in cases if case.id in ids]
    if args.limit:
        cases = cases[: args.limit]

    if not cases:
        print("没有匹配的评测用例", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"fixture: {fixture}")
        print(f"用例: {len(cases)}")
        for case in cases:
            print(f"  - {case.id} [{case.category}] {case.role}")
        return 0

    from mclaw.agents.presets import get_preset_by_id

    missing = sorted({case.role for case in cases if get_preset_by_id(case.role) is None})
    if missing:
        print(f"未知岗位预设: {', '.join(missing)}", file=sys.stderr)
        return 1

    km = None
    if not args.no_kb:
        from mclaw.memory.knowledge_manager import KnowledgeManager

        km = KnowledgeManager(
            data_dir=kb_dir,
            vector_backend="off",
            reranker_enabled=False,
        )

    baseline = None
    if not args.no_baseline:
        if args.baseline:
            try:
                baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"读取基线失败，按无基线继续: {exc}", file=sys.stderr)
        else:
            baseline = load_latest_run(output_dir)

    client = LLMClient()
    result = await run_golden_cases(
        cases,
        client=client,
        km=km,
        concurrency=args.concurrency,
        use_kb=not args.no_kb,
    )
    result.fixture = str(fixture)
    comparison = compare_runs(baseline, result, max_drop=args.max_drop)
    result.comparison = comparison
    path = save_run(result, output_dir)

    if args.json:
        payload = result.to_dict()
        payload["comparison"] = comparison
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(result)
        if comparison.get("delta") is not None:
            print(
                f"\n基线对比: {comparison['baseline_pass_rate']:.1%} -> "
                f"{comparison['current_pass_rate']:.1%} "
                f"({comparison['delta']:+.1%})"
            )
            if comparison.get("role_drops"):
                print("跌破阈值的岗位:")
                for role, delta in comparison["role_drops"].items():
                    print(f"  - {role}: {delta:+.1%}")
        print(f"\n结果已写入: {path}")

    if args.fail_under > 0 and result.pass_rate < args.fail_under:
        print(
            f"通过率 {result.pass_rate:.1%} 低于阈值 {args.fail_under:.1%}",
            file=sys.stderr,
        )
        return 1
    if comparison.get("dropped") and not args.no_fail_on_drop:
        print(
            f"通过率较基线下跌 {comparison['delta']:+.1%}，超过允许跌幅 "
            f"{args.max_drop:.1%}",
            file=sys.stderr,
        )
        return 1
    if result.total and all(case.get("error") for case in result.cases):
        print("所有用例执行失败，请检查 LLM 端点配置", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="企业运营 8 岗位 L1 黄金集评测")
    parser.add_argument("--fixture", type=Path, default=None, help="黄金集 JSON 路径")
    parser.add_argument("--roles", default="", help="按岗位过滤，逗号分隔")
    parser.add_argument("--categories", default="", help="按类别过滤，逗号分隔")
    parser.add_argument("--ids", default="", help="按用例 id 过滤，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--concurrency", type=int, default=3, help="并发数（默认 3）")
    parser.add_argument("--no-kb", action="store_true", help="不检索知识库，只测岗位卡")
    parser.add_argument("--data-dir", type=Path, default=None, help="知识库数据目录")
    parser.add_argument("--output-dir", type=Path, default=None, help="评测结果目录")
    parser.add_argument("--fail-under", type=float, default=0.0, help="通过率低于该值时退出码 1")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="基线 run JSON；默认使用输出目录的 latest.json（上一次运行）",
    )
    parser.add_argument("--max-drop", type=float, default=0.05, help="允许的最大通过率跌幅（默认 5%%）")
    parser.add_argument("--no-baseline", action="store_true", help="不与上一次运行比较")
    parser.add_argument(
        "--no-fail-on-drop",
        action="store_true",
        help="通过率跌破基线时仍返回 0（只告警，不失败）",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只列出匹配用例，不调用 LLM")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
