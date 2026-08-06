"""Phase 3 — keyword matching DSL parser/matcher.

Covers the frequency.py semantics plus the §13.2 deepcopy hardening
(no downstream mutation leaks back into the parsed model).
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from finpulse_frequency import (
    MAX_GROUPS,
    MAX_TOKENS_PER_GROUP,
    compile_matcher,
    parse_rules,
    snapshot_rules,
    validate_token,
)


# ── parse_rules ────────────────────────────────────────────────────────


class TestParseRules:
    def test_single_group_mixed_tokens(self) -> None:
        rules = parse_rules(
            """
            +美联储
            加息
            !广告
            @fed
            """.strip()
        )
        assert len(rules.groups) == 1
        g = rules.groups[0]
        assert g.required == ["美联储"]
        assert g.normal == ["加息"]
        assert g.aliases == ["fed"]
        assert rules.filter_words == ["广告"]

    def test_blank_line_splits_groups(self) -> None:
        text = "+美联储\n加息\n\n+欧央行\n降息"
        rules = parse_rules(text)
        assert len(rules.groups) == 2
        assert rules.groups[0].required == ["美联储"]
        assert rules.groups[1].required == ["欧央行"]

    def test_global_filter_section(self) -> None:
        text = "+A\n\n[GLOBAL_FILTER]\n广告\n带货\n[END]\n\n+B"
        rules = parse_rules(text)
        assert rules.global_filters == ["广告", "带货"]
        assert len(rules.groups) == 2
        assert rules.groups[0].required == ["A"]
        assert rules.groups[1].required == ["B"]

    def test_comments_are_ignored(self) -> None:
        text = "# a comment\n+Fed\n# another"
        rules = parse_rules(text)
        assert rules.groups[0].required == ["Fed"]

    def test_empty_text_is_empty_rules(self) -> None:
        rules = parse_rules("")
        assert rules.groups == []
        assert rules.filter_words == []
        assert rules.global_filters == []

    def test_respects_max_groups(self) -> None:
        body = "\n\n".join([f"+g{i}" for i in range(MAX_GROUPS + 20)])
        rules = parse_rules(body)
        assert len(rules.groups) == MAX_GROUPS

    def test_respects_max_tokens_per_group(self) -> None:
        body = "+head\n" + "\n".join([f"x{i}" for i in range(MAX_TOKENS_PER_GROUP + 5)])
        rules = parse_rules(body)
        assert len(rules.groups[0].normal) == MAX_TOKENS_PER_GROUP


# ── deepcopy hardening ───────────────────────────────────────────────


class TestSnapshotRules:
    def test_independent_from_source(self) -> None:
        rules = parse_rules("+Fed\n\n+ECB\n!noise")
        snap = snapshot_rules(rules)
        snap.filter_words.append("MUTATED")
        snap.groups[0].required.append("injected")
        assert rules.filter_words == ["noise"]
        assert rules.groups[0].required == ["Fed"]


# ── FrequencyMatcher ────────────────────────────────────────────────


class TestFrequencyMatcher:
    def test_empty_rules_matches_everything(self) -> None:
        m = compile_matcher("")
        assert m.match("anything") is True
        assert m.match("") is False  # empty title always fails

    def test_required_and_normal_combined(self) -> None:
        m = compile_matcher("+美联储\n加息")
        assert m.match("美联储宣布加息 25 个基点") is True
        # Missing the required token → no match.
        assert m.match("央行讨论加息可能性") is False
        # Required present but no normal present → no match.
        assert m.match("美联储主席今日发表讲话") is False

    def test_global_filter_short_circuits(self) -> None:
        m = compile_matcher("+A\n\n[GLOBAL_FILTER]\n广告")
        assert m.match("A 相关重大消息") is True
        assert m.match("A 广告 置顶") is False

    def test_block_list_excludes_hit(self) -> None:
        m = compile_matcher("+A\n!广告")
        assert m.match("A 新闻") is True
        assert m.match("A 广告篇") is False

    def test_multi_group_or(self) -> None:
        m = compile_matcher("+Fed\n\n+ECB")
        assert m.match("Fed news") is True
        assert m.match("ECB briefing") is True
        assert m.match("BoJ press conference") is False

    def test_matched_terms_returns_all_hits(self) -> None:
        m = compile_matcher("+Fed\n加息\n\n+ECB")
        hits = m.matched_terms("Fed announced 加息 and the ECB responded")
        assert set(hits) == {"Fed", "加息", "ECB"}


class TestValidateToken:
    def test_accepts_short_line(self) -> None:
        assert validate_token("+美联储") is True

    def test_rejects_multiline(self) -> None:
        assert validate_token("a\nb") is False

    def test_rejects_empty(self) -> None:
        assert validate_token("") is False

    def test_rejects_too_long(self) -> None:
        assert validate_token("x" * 100) is False
