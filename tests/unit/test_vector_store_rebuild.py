"""L1 Unit Tests: memories 向量库在 embedding 模型切换后的重建流程。

背景：向量维度由 embedding 模型决定（text2vec=768 维 → bge-small=512 维）。
模型指纹不一致时 VectorStore 会清空 collection，然后把 MemoryManager 喂进来的
记忆重新嵌入；只有全部成功才清除 ``reindex_pending``，否则留到下次启动重试。
"""

from __future__ import annotations

import threading
import time

from mclaw.memory.vector_store import VectorStore, needs_vector_rebuild


def _bare_store(model: str = "BAAI/bge-small-zh-v1.5") -> VectorStore:
    """绕过 __init__ 构造实例：构造器会拉起后台初始化线程，单测不需要。"""
    vs = VectorStore.__new__(VectorStore)
    vs.model_name = model
    vs._lock = threading.RLock()
    vs._collection = None
    vs._needs_rebuild = True
    vs._rebuild_started = False
    vs._rebuild_provider = None
    return vs


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestNeedsRebuild:
    def test_legacy_vectors_without_fingerprint(self):
        assert needs_vector_rebuild({}, 7, "BAAI/bge-small-zh-v1.5")

    def test_empty_collection_without_fingerprint(self):
        assert not needs_vector_rebuild({}, 0, "BAAI/bge-small-zh-v1.5")

    def test_switch_from_text2vec(self):
        meta = {"embedding_model": "shibing624/text2vec-base-chinese"}
        assert needs_vector_rebuild(meta, 7, "BAAI/bge-small-zh-v1.5")

    def test_same_model_keeps_index(self):
        meta = {"embedding_model": "BAAI/bge-small-zh-v1.5"}
        assert not needs_vector_rebuild(meta, 7, "BAAI/bge-small-zh-v1.5")

    def test_pending_flag_survives_restart(self):
        meta = {"embedding_model": "BAAI/bge-small-zh-v1.5", "reindex_pending": True}
        assert needs_vector_rebuild(meta, 0, "BAAI/bge-small-zh-v1.5")


class TestRebuild:
    def test_full_success_clears_pending(self, monkeypatch):
        vs = _bare_store()
        monkeypatch.setattr(vs, "batch_add", lambda items: len(items))

        assert vs.rebuild([{"id": "m1"}]) == 1
        assert vs.needs_rebuild is False

    def test_partial_failure_keeps_pending(self, monkeypatch):
        vs = _bare_store()
        monkeypatch.setattr(vs, "batch_add", lambda items: len(items) - 1)

        assert vs.rebuild([{"id": f"m{i}"} for i in range(3)]) == 2
        assert vs.needs_rebuild is True

    def test_empty_rebuild_clears_pending(self, monkeypatch):
        vs = _bare_store()
        monkeypatch.setattr(vs, "batch_add", lambda items: 0)

        assert vs.rebuild([]) == 0
        assert vs.needs_rebuild is False


class TestRebuildProvider:
    def test_provider_injected_after_init_triggers_rebuild(self, monkeypatch):
        # init 在后台线程跑，provider 往往在之后才注入：两条路径都要能启动重建
        vs = _bare_store()
        monkeypatch.setattr(vs, "batch_add", lambda items: len(items))

        vs.set_rebuild_provider(lambda: [{"id": "m1"}, {"id": "m2"}])

        assert _wait_until(lambda: vs.needs_rebuild is False)

    def test_missing_provider_defers_without_crashing(self):
        vs = _bare_store()
        vs._start_background_rebuild()  # 不应启动，也不应抛
        assert vs._rebuild_started is False

    def test_provider_error_does_not_stick(self, monkeypatch):
        def _explode():
            raise RuntimeError("读库失败")

        vs = _bare_store()
        vs._rebuild_provider = _explode
        vs._start_background_rebuild()

        assert _wait_until(lambda: vs._rebuild_started is False)
