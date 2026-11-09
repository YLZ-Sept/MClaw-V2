"""在线 reranker API 风格与白名单测试（不发起真实网络请求）"""

from __future__ import annotations

from mclaw.memory import knowledge_manager as km_module
from mclaw.memory.knowledge_manager import KnowledgeManager
from mclaw.memory.reranker import Reranker


class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data: dict, captured: dict):
        self._data = data
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self._captured["url"] = url
        self._captured["json"] = json
        return _FakeResponse(self._data)


def test_dashscope_style_payload_and_scores(monkeypatch):
    captured: dict = {}
    data = {
        "output": {
            "results": [
                {"index": 1, "relevance_score": 0.91},
                {"index": 0, "relevance_score": 0.12},
            ]
        }
    }
    monkeypatch.setattr(
        "httpx.Client", lambda *a, **k: _FakeClient(data, captured)
    )
    reranker = Reranker(
        api_base="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        api_key="k",
        api_model="gte-rerank-v2",
        api_style="dashscope",
        timeout=5,
    )
    reranker._available = True

    scores = reranker.rerank("查询", ["文档A", "文档B"])

    assert scores == [0.12, 0.91]
    assert captured["url"].endswith("/text-rerank/text-rerank")
    assert captured["json"]["input"]["query"] == "查询"
    assert captured["json"]["input"]["documents"] == ["文档A", "文档B"]


def test_siliconflow_style_appends_rerank_path(monkeypatch):
    captured: dict = {}
    data = {"results": [{"index": 0, "relevance_score": 0.88}]}
    monkeypatch.setattr(
        "httpx.Client", lambda *a, **k: _FakeClient(data, captured)
    )
    reranker = Reranker(
        api_base="https://api.siliconflow.cn/v1",
        api_key="k",
        api_model="BAAI/bge-reranker-v2-m3",
        api_style="auto",
    )
    reranker._available = True

    scores = reranker.rerank("查询", ["文档A"])

    assert scores == [0.88]
    assert captured["url"] == "https://api.siliconflow.cn/v1/rerank"


class TestOnlineRerankWhitelist:
    def _km(self, tmp_path, monkeypatch, **kwargs):
        monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
        return KnowledgeManager(
            data_dir=tmp_path, vector_backend="off", reranker_enabled=False, **kwargs
        )

    def test_whitelist_by_name_and_id(self, tmp_path, monkeypatch):
        km = self._km(
            tmp_path,
            monkeypatch,
            reranker_collections="招投标采集结果,9c0f8058-77d",
        )
        public = km.create_collection("招投标采集结果", "")
        by_id = km.create_collection("按ID放行", "")
        km._reranker_collections.add(by_id.id)

        assert km._rerank_allowed(public.id)
        assert km._rerank_allowed(by_id.id)
        assert not km._rerank_allowed(None)

    def test_empty_whitelist_disables_online(self, tmp_path, monkeypatch):
        km = self._km(tmp_path, monkeypatch)
        coll = km.create_collection("财务部", "")
        assert not km._rerank_allowed(coll.id)
