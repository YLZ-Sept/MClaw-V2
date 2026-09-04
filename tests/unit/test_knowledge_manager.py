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
from mclaw.memory.knowledge_manager import KnowledgeManager
from mclaw.memory.knowledge_types import KnowledgeChunk, SearchResult, _short_uuid


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
