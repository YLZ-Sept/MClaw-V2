"""L1 Unit Tests: 知识库查询扩展（同义词/缩略语代偿）。

背景：未装 vector-memory 可选模块（约 2.5GB）时知识库只有 BM25 关键词检索，
问"等保"而文档写"等级保护测评"就一条也搜不到。本模块用零依赖方式补召回。

关键回归点（真实数据实测）：扩展词**不能**直接 OR 进原查询——那样 BM25 会按
命中词数把无关 chunk 排到前面，关键词明确的问题从 10/10 掉到 8/10。必须是
"原查询为主 + 扩展降权补召回"的两级融合。
"""

from __future__ import annotations

import json

from mclaw.memory.knowledge_manager import KnowledgeManager
from mclaw.memory.query_expansion import (
    expand_query,
    load_synonym_groups,
)


class TestSynonymTable:
    def test_builtin_table_loads(self):
        groups = load_synonym_groups()
        assert groups, "内置同义词表应能加载"
        assert any("等保" in g and "等级保护" in g for g in groups)

    def test_missing_user_table_is_ignored(self, tmp_path):
        groups = load_synonym_groups(tmp_path / "nope.json")
        assert groups, "用户表不存在时应回退到内置表"

    def test_user_table_appends_without_losing_builtins(self, tmp_path):
        # 用户加自己的词时不该把内置同义词挤掉（那等于静默降低召回）
        user = tmp_path / "synonyms.json"
        user.write_text(
            json.dumps({"等保": ["云南省等级保护"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        groups = load_synonym_groups(user)
        merged = next(g for g in groups if "云南省等级保护" in g)
        assert "等保" in merged
        assert "等级保护" in merged, "内置同义词不该被用户表覆盖掉"
        assert "等保测评" in merged

    def test_comment_keys_are_skipped(self, tmp_path):
        user = tmp_path / "synonyms.json"
        user.write_text(json.dumps({"_comment": "说明", "甲": ["乙"]}), encoding="utf-8")
        groups = load_synonym_groups(user)
        assert not any("说明" in g for g in groups)

    def test_broken_json_does_not_raise(self, tmp_path):
        user = tmp_path / "synonyms.json"
        user.write_text("{ 不是合法 json", encoding="utf-8")
        assert load_synonym_groups(user)  # 回退内置表


class TestExpandQuery:
    def test_expands_known_term(self):
        groups = load_synonym_groups()
        extra = expand_query("等保测评的项目有哪些", groups)
        assert "等级保护" in extra
        assert "等保测评的项目有哪些" not in extra  # 只返回补充词

    def test_no_match_returns_empty(self):
        groups = load_synonym_groups()
        assert expand_query("今天天气怎么样", groups) == []

    def test_matches_multi_token_term_in_raw_query(self):
        # "等级保护测评" 会被 jieba 切成多段，必须在原串上匹配才不漏
        groups = load_synonym_groups()
        extra = expand_query("等级保护测评服务采购", groups)
        assert "等保" in extra

    def test_respects_max_extra(self):
        groups = load_synonym_groups()
        extra = expand_query("等保 密评 HVV 供应商 值班 中标", groups, max_extra=3)
        assert len(extra) <= 3


class TestExpansionMerge:
    """两级融合：原查询命中永远排在"只靠同义词命中"之前。"""

    def test_primary_always_outranks_expansion_only(self):
        primary = {"a": 0.9, "b": 0.8}
        extra = {"x": 0.95, "y": 0.94}
        merged = KnowledgeManager._merge_expansion(primary, extra)

        assert set(merged) == {"a", "b", "x", "y"}
        assert merged["b"] > merged["x"], "原查询的最后一名也要高于扩展命中的第一名"

    def test_overlapping_ids_keep_primary_score(self):
        merged = KnowledgeManager._merge_expansion({"a": 0.9}, {"a": 0.1})
        assert merged["a"] == 1.0 / 61  # 按原查询的第 1 名计分，不叠加

    def test_weight_zero_means_no_recall_gain(self):
        merged = KnowledgeManager._merge_expansion({"a": 1.0}, {"x": 1.0}, weight=0.0)
        assert merged["x"] == 0.0


class TestQueryExpansionWiring:
    """KnowledgeManager 侧：扩展词要按索引同一套分词切好，且可配置关闭。"""

    def _km(self, tmp_path, monkeypatch, **kwargs):
        from mclaw.memory import knowledge_manager as km_module

        monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
        return KnowledgeManager(data_dir=tmp_path, **kwargs)

    def test_expansion_terms_are_segmented(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch)
        terms = km._expansion_terms("等保测评服务")
        assert terms, "应产出扩展词"
        # 整词"等级保护测评"必须被切碎，否则与索引里的 token 对不上
        assert "等级保护测评" not in terms

    def test_disabled_returns_no_terms(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch)
        km._expand_query_enabled = False
        assert km._expansion_terms("等保测评服务") == []

    def test_segment_is_consistent_between_index_and_query(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch)
        assert km._segment("等级保护测评") == km._tokenize_for_fts("等级保护测评")

    def test_search_still_works_with_expansion(self, tmp_path, monkeypatch):
        from tests.unit.test_knowledge_manager import _insert_doc_and_chunk

        km = self._km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "网络安全等级保护测评服务采购项目")

        results = km.search_with_chunks("等保测评项目", collection_id=coll.id, top_k=5)
        assert results, "靠同义词扩展应能召回（原查询里没有『等级保护』这些词）"
