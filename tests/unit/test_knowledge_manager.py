"""L1 Unit Tests: 知识库检索（KnowledgeManager）与 prompt 自动注入。

覆盖两个回归点：
1. ``search_with_chunks`` 应通过 JOIN 关联 ``kb_documents`` 并把来源文件名填进
   ``chunk.metadata["filename"]``（此前搜索结果来源一直是「未知文档」）。
2. ``prompt.builder._build_knowledge_section`` 应使用 ``SearchResult`` 的 dataclass
   属性访问（``r.score`` / ``r.chunk``），而不是 dict 的 ``.get()``（此前会抛
   ``AttributeError`` 被静默吞掉，导致自动注入永远返回空字符串）。
"""

from __future__ import annotations

import time

from mclaw.memory import knowledge_manager as km_module
from mclaw.memory import reranker as reranker_module
from mclaw.memory.knowledge_manager import KnowledgeManager
from mclaw.memory.knowledge_types import KnowledgeChunk, SearchResult, _short_uuid
from mclaw.memory.vector_store import needs_vector_rebuild


def _stub_reranker(monkeypatch, created: dict | None = None):
    """替换 Reranker 为不加载模型的桩，返回可观察的构造参数"""

    class _StubReranker:
        def __init__(self, **kwargs):
            if created is not None:
                created.update(kwargs)
            self.available = True
            self.started = False

        def start_background_init(self):
            self.started = True

    monkeypatch.setattr(reranker_module, "Reranker", _StubReranker)
    return _StubReranker


def _make_km(tmp_path, monkeypatch) -> KnowledgeManager:
    """构造 KnowledgeManager，关闭 ChromaDB 后台加载，只走 FTS5 路径。"""
    monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
    return KnowledgeManager(data_dir=tmp_path)


def _insert_doc_and_chunk(km: KnowledgeManager, collection_id: str, filename: str, content: str) -> str:
    """直接插入一个 document + chunk + FTS 记录，绕过文件摄入。"""
    conn = km._get_conn()
    now = time.time()
    doc_id = _short_uuid()
    chunk_id = _short_uuid()
    conn.execute(
        "INSERT INTO kb_documents (id, collection_id, filename, file_type, checksum, "
        "status, chunk_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, collection_id, filename, "txt", "x", "ready", 1, now, now),
    )
    conn.execute(
        "INSERT INTO kb_chunks (id, doc_id, collection_id, content, chunk_index, token_count) "
        "VALUES (?,?,?,?,?,?)",
        (chunk_id, doc_id, collection_id, content, 0, len(content)),
    )
    tokenized = km._tokenize_for_fts(content)
    conn.execute(
        "INSERT INTO kb_chunks_fts (rowid, content) VALUES "
        "((SELECT rowid FROM kb_chunks WHERE id=?), ?)",
        (chunk_id, tokenized),
    )
    conn.commit()
    conn.close()
    return chunk_id


class TestEnsureCollection:
    """D 轮：SYSTEM 预置空知识库骨架 —— ensure_collection 幂等、稳定 id。"""

    def test_creates_once_and_is_idempotent(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        created = km.ensure_collection(
            "sys-dept-finance", "财务部资料库", "部门空骨架", workspace_id="default"
        )
        assert created is True

        again = km.ensure_collection(
            "sys-dept-finance", "财务部资料库", "部门空骨架", workspace_id="default"
        )
        assert again is False  # 已存在，不重复建

        coll = km.get_collection("sys-dept-finance")
        assert coll is not None
        assert coll.id == "sys-dept-finance"
        assert coll.name == "财务部资料库"
        assert coll.workspace_id == "default"
        assert coll.doc_count == 0
        assert coll.chunk_count == 0

    def test_does_not_touch_existing_uuid_collections(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        auto = km.create_collection("用户集合", "自动 id")
        # 已有 uuid 集合不受 ensure 影响，数量不变
        assert km.ensure_collection("sys-dept-tech", "技术中心资料库", "骨架") is True
        assert km.get_collection(auto.id) is not None
        _items, total = km.list_collections()
        # auto + sys-dept-tech = 2
        assert total == 2


class TestSearchWithChunks:
    def test_fills_filename_from_document(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "hello.pdf", "hello world knowledge base")

        results = km.search_with_chunks("knowledge", collection_id=coll.id, top_k=5)

        assert len(results) == 1
        assert results[0].chunk.content == "hello world knowledge base"
        assert results[0].chunk.metadata["filename"] == "hello.pdf"


class TestBuildKnowledgeSection:
    class _FakeKM:
        def search_with_chunks(self, query, collection_id=None, top_k=10):
            chunk = KnowledgeChunk(
                id="c1",
                doc_id="d1",
                collection_id="col1",
                content="知识库关键片段",
                metadata={"filename": "测试文档.txt"},
            )
            return [SearchResult(chunk=chunk, score=0.9, bm25_score=0.8)]

    def test_uses_dataclass_attrs_and_includes_filename(self, monkeypatch):
        from mclaw.api.routes import knowledge as kr

        monkeypatch.setattr(kr, "_knowledge_manager", self._FakeKM())

        from mclaw.prompt.builder import _build_knowledge_section

        out = _build_knowledge_section("查询", knowledge_collections=["col1"])
        assert "知识库关键片段" in out
        assert "测试文档.txt" in out

    def test_empty_collections_returns_empty(self):
        from mclaw.prompt.builder import _build_knowledge_section

        # 无绑定集合时应直接返回空字符串（get_knowledge_manager 未初始化也会被吞掉）
        assert _build_knowledge_section("查询", knowledge_collections=[]) == ""


class TestInjectionBudgetAllocation:
    """注入预算分配：保底配额 + 不硬切半句。

    回归点（真实数据实测）：某 chunk 全长 592 字、关键数字在第 528 字处。
    把预算按条数均分成 497 字后，答案被截掉——本来能答对的问题反而答错。
    """

    @staticmethod
    def _km_returning(chunks: list[str]):
        class _FakeKM:
            def search_with_chunks(self, query, collection_id=None, top_k=10):
                out = []
                for i, text in enumerate(chunks):
                    out.append(
                        SearchResult(
                            chunk=KnowledgeChunk(
                                id=f"c{i}",
                                doc_id="d1",
                                collection_id="col1",
                                content=text,
                                metadata={"filename": "f.txt"},
                            ),
                            score=1.0 - i * 0.1,
                            bm25_score=1.0 - i * 0.1,
                        )
                    )
                return out

        return _FakeKM()

    def _render(self, monkeypatch, chunks, **kwargs):
        from mclaw.api.routes import knowledge as kr

        monkeypatch.setattr(kr, "_knowledge_manager", self._km_returning(chunks))
        from mclaw.prompt.builder import _build_knowledge_section

        return _build_knowledge_section(
            "查询", knowledge_collections=["col1"], **kwargs
        )

    def test_answer_buried_mid_chunk_survives(self, monkeypatch):
        # 答案在第 528 字（共 592 字）——均分预算会把它切掉
        long_chunk = "填充" * 264 + "关键数字22.50万元" + "尾部"
        out = self._render(monkeypatch, [long_chunk])

        assert "关键数字22.50万元" in out

    def test_respects_max_chars(self, monkeypatch):
        chunks = ["段落" * 400 for _ in range(6)]
        out = self._render(monkeypatch, chunks, max_chunks=4, max_chars=3200)

        assert len(out) <= 3200

    def test_widens_window_beyond_top_two(self, monkeypatch):
        # 第 4 名的 chunk 也要有机会进 prompt（此前每集合只取 2 条）
        chunks = ["无关内容"] * 3 + ["第四名的答案42"]
        out = self._render(monkeypatch, chunks, max_chunks=4)

        assert "第四名的答案42" in out

    def test_small_budget_keeps_fewer_chunks_intact(self, monkeypatch):
        # 预算装不下 3 条保底时减少条数，而不是把每条都切成半句
        chunks = ["甲" * 1000 + "甲尾", "乙" * 1000 + "乙尾", "丙" * 1000 + "丙尾"]
        out = self._render(monkeypatch, chunks, max_chunks=3, max_chars=1500)

        assert len(out) <= 1500
        assert "甲尾" in out, "首条应完整保留"
        assert "乙" not in out and "丙" not in out, "后两条整体不注入，而不是各切一半"

    def test_default_budget_actually_fits_default_chunks(self, monkeypatch):
        # 注入条数与预算必须自洽：否则"答案排到第 6 名"的那批问题仍然进不来
        chunks = ["内容" * 500 for _ in range(8)]
        out = self._render(monkeypatch, chunks)

        assert out.count("[来源:") == 6, "默认 max_chunks=6 必须真的装得下 6 条"
        assert len(out) <= 5200


class TestRerankerIntegration:
    """Reranker 精排层：就绪时重排序、未就绪时沿用 RRF、失败时安全回退。"""

    def test_no_reranker_keeps_rrf_order(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "alpha common word")
        _insert_doc_and_chunk(km, coll.id, "b.txt", "beta common word")

        # 未设置 reranker（纯 FTS5 模式），结果不应带 +rerank 标记
        results = km.search_with_chunks("common", collection_id=coll.id, top_k=5)
        assert len(results) == 2
        for r in results:
            assert not r.source_type.endswith("+rerank")

    def test_reranker_reorders_by_relevance(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "alpha common word")
        _insert_doc_and_chunk(km, coll.id, "b.txt", "beta common gold word")
        _insert_doc_and_chunk(km, coll.id, "c.txt", "gamma common word")

        class _FakeReranker:
            available = True

            def rerank(self, query, texts):
                # 含 "gold" 的候选给最高分
                return [1.0 if "gold" in t else 0.2 for t in texts]

        km._reranker = _FakeReranker()

        results = km.search_with_chunks("common", collection_id=coll.id, top_k=3)
        assert len(results) == 3
        # 全部结果应标记 +rerank，且 top1 是含 gold 的 chunk
        for r in results:
            assert r.source_type.endswith("+rerank")
        assert "gold" in results[0].chunk.content
        assert results[0].score == 1.0
        assert results[0].score >= results[1].score >= results[2].score

    def test_reranker_failure_falls_back_to_rrf(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "alpha common word")
        _insert_doc_and_chunk(km, coll.id, "b.txt", "beta common word")

        class _BrokenReranker:
            available = True

            def rerank(self, query, texts):
                raise RuntimeError("boom")

        km._reranker = _BrokenReranker()

        # 精排抛异常时应安全回退到 RRF 排序，不抛出
        results = km.search_with_chunks("common", collection_id=coll.id, top_k=5)
        assert len(results) == 2
        for r in results:
            assert not r.source_type.endswith("+rerank")

    def test_unavailable_reranker_skipped(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "alpha common word")

        class _LoadingReranker:
            available = False  # 模型还在后台加载

            def rerank(self, query, texts):
                raise AssertionError("不应被调用")

        km._reranker = _LoadingReranker()

        results = km.search_with_chunks("common", collection_id=coll.id, top_k=5)
        assert len(results) == 1


class TestReindexAll:
    """全量重建：空库零文档、单文档重建。"""

    def test_reindex_all_empty(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        stats = km.reindex_all()
        assert stats == {"total": 0, "success": 0, "failed": 0}

    def test_reindex_all_missing_file_marks_error(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        # 直接插入一条 file_path 已丢失的文档记录
        conn = km._get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO kb_documents (id, collection_id, filename, file_path, file_type, "
            "checksum, status, chunk_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("d1", coll.id, "lost.txt", str(tmp_path / "nope.txt"), "txt", "x",
             "ready", 0, now, now),
        )
        conn.commit()
        conn.close()

        stats = km.reindex_all()
        assert stats["total"] == 1
        assert stats["failed"] == 1
        assert stats["success"] == 0
        # 文档状态应被标记为 error
        doc = km.get_document("d1")
        assert doc is not None
        assert doc.status == "error"


class TestVectorRebuildDecision:
    """embedding 模型切换 / 旧版无指纹存量向量 → 必须重建。

    回归点：旧版建的 collection 没有 embedding_model 指纹，而默认模型已从
    text2vec(768 维) 换成 bge-small(512 维)。若只补记指纹不重建，向量查询
    会因维度不符抛错并被静默吞掉，检索退化成纯 FTS5 且新文档写不进向量。
    """

    MODEL = "BAAI/bge-small-zh-v1.5"

    def test_empty_collection_without_fingerprint_only_stamps(self):
        # 空库没有数据可重建，只需补记指纹
        assert not needs_vector_rebuild({}, 0, self.MODEL)
        assert not needs_vector_rebuild({"hnsw:space": "cosine"}, 0, self.MODEL)

    def test_legacy_collection_with_vectors_needs_rebuild(self):
        # 有数据但无指纹 = 旧版存量向量，模型未知且与当前默认模型维度不符
        assert needs_vector_rebuild({"hnsw:space": "cosine"}, 42, self.MODEL)

    def test_model_switch_needs_rebuild(self):
        meta = {"embedding_model": "shibing624/text2vec-base-chinese"}
        assert needs_vector_rebuild(meta, 42, self.MODEL)

    def test_same_model_keeps_vectors(self):
        meta = {"embedding_model": self.MODEL}
        assert not needs_vector_rebuild(meta, 42, self.MODEL)

    def test_pending_flag_retries_on_restart(self):
        # 上次重建中途退出：指纹已经是当前模型，但 pending 未清
        meta = {"embedding_model": self.MODEL, "reindex_pending": True}
        assert needs_vector_rebuild(meta, 5, self.MODEL)
        meta["reindex_pending"] = False
        assert not needs_vector_rebuild(meta, 5, self.MODEL)


class TestReindexStatus:
    """全量重建的触发入口与进度状态（供 POST /api/knowledge/reindex 使用）。"""

    def test_schedule_reindex_reports_progress_and_last_stats(self, tmp_path, monkeypatch):
        km = _make_km(tmp_path, monkeypatch)
        coll = km.create_collection("技术文档", "测试集合")
        _insert_doc_and_chunk(km, coll.id, "a.txt", "alpha content")

        assert km.reindex_status()["running"] is False

        assert km.schedule_reindex_all(reason="单测") is True

        # 等后台线程收尾（重建本身很快）
        for _ in range(100):
            if not km.reindex_status()["running"]:
                break
            time.sleep(0.05)

        status = km.reindex_status()
        assert status["running"] is False
        assert status["model"] == km.model_name
        assert status["last"]["ok"] is True
        assert status["last"]["total"] == 1
        assert status["last"]["reason"] == "单测"

    def test_rerank_candidates_cap_is_small(self):
        # 候选数是 CPU 上精排延迟的直接乘数，别再调回 30
        assert 0 < KnowledgeManager.RERANK_CANDIDATES <= 20


class TestRerankerToggle:
    """精排开关三态：默认不下载本地模型（~1.1GB），只在配了在线 API 时自动启用。

    回归点：早期实现默认 True，任何装有机型的首次启动都会在后台静默下载
    bge-reranker-base 并常驻内存，对离线安装包不友好。
    """

    def _km(self, tmp_path, monkeypatch, **kwargs) -> KnowledgeManager:
        monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
        # 隔离宿主 .env：不让外部 KB_RERANKER_* 影响判定
        monkeypatch.setattr(km_module, "_settings_value", lambda key, fallback: fallback)
        monkeypatch.setattr(km_module, "_settings_opt_bool", lambda key, fallback=None: fallback)
        _stub_reranker(monkeypatch)  # 别在单测里真去加载模型
        return KnowledgeManager(data_dir=tmp_path, **kwargs)

    def test_default_off_without_api(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch)
        assert km.reranker_enabled is False

    def test_api_config_enables_automatically(self, tmp_path, monkeypatch):
        km = self._km(
            tmp_path,
            monkeypatch,
            reranker_api_base="https://api.siliconflow.cn/v1",
            reranker_api_key="sk-x",
        )
        assert km.reranker_enabled is True

    def test_api_base_without_key_stays_off(self, tmp_path, monkeypatch):
        # 只有 base 没有 key 打不通接口，不该启用
        km = self._km(
            tmp_path, monkeypatch, reranker_api_base="https://api.siliconflow.cn/v1"
        )
        assert km.reranker_enabled is False

    def test_explicit_true_enables_local_model(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch, reranker_enabled=True)
        assert km.reranker_enabled is True

    def test_explicit_false_wins_over_api_config(self, tmp_path, monkeypatch):
        km = self._km(
            tmp_path,
            monkeypatch,
            reranker_enabled=False,
            reranker_api_base="https://api.siliconflow.cn/v1",
            reranker_api_key="sk-x",
        )
        assert km.reranker_enabled is False


class TestRerankerDecoupledFromVectorMode:
    """精排不再依赖 ChromaDB —— 纯 FTS5（没装 vector-memory）也要能装配。

    回归点：装配逻辑原本写在 ``_do_chroma_init`` 的成功路径里，而该函数在
    chromadb 缺失时提前 return。结果是没装那个约 2.5GB 可选模块的机器永远
    拿不到精排，哪怕配置的是零依赖的在线 Rerank API。
    """

    def test_reranker_assembled_without_chromadb(self, tmp_path, monkeypatch):
        created: dict = {}
        _stub_reranker(monkeypatch, created)
        monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
        monkeypatch.setattr(km_module, "_settings_value", lambda key, fallback: fallback)

        km = KnowledgeManager(data_dir=tmp_path, reranker_enabled=True)

        for _ in range(150):
            if km._reranker is not None:
                break
            time.sleep(0.02)

        assert km._reranker is not None, "纯 FTS5 模式下也应装配精排"
        assert km._vector_enabled is False, "前提：确实没进向量模式"
        assert created["api_base"] == ""  # 默认走本地模型配置

    def test_api_mode_reranker_available_in_fts5(self, tmp_path, monkeypatch):
        _stub_reranker(monkeypatch)
        monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
        monkeypatch.setattr(km_module, "_settings_value", lambda key, fallback: fallback)

        km = KnowledgeManager(
            data_dir=tmp_path,
            reranker_api_base="https://api.siliconflow.cn/v1",
            reranker_api_key="sk-x",
        )
        for _ in range(150):
            if km._reranker is not None:
                break
            time.sleep(0.02)

        assert km._vector_enabled is False
        assert km._reranker is not None
        assert km._reranker.started is True
