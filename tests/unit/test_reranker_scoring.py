"""L1 Unit Tests: Reranker 精排的分数语义、多源委托与安全降级。

覆盖两个回归点：
1. 本地 cross-encoder 输出的是原始 logit（bge-reranker 系列无激活函数），
   而在线 Rerank API 返回 0-1 的 relevance_score。这个分数会直接进到给
   LLM 的提示词里（``相关度: {score:.2f}``），不归一就会出现 "-4.21"。
2. 本地加载必须走 ``model_hub.load_cross_encoder``：早期实现直接调
   ``CrossEncoder(模型名)``，在下载源解析为 modelscope 的机器上会绕过名称
   映射直连 huggingface.co，而那正是"HuggingFace 不通才选 ModelScope"的场景，
   必然下载失败、精排静默失效。
"""

from __future__ import annotations

from mclaw.memory import model_hub
from mclaw.memory.reranker import Reranker, _sigmoid


class _FakeCrossEncoder:
    def __init__(self, scores: list[float] | None = None, raise_on_predict: bool = False):
        self._scores = scores if scores is not None else []
        self._raise = raise_on_predict

    def predict(self, pairs, show_progress_bar=False):
        if self._raise:
            raise RuntimeError("boom")
        return self._scores


def _ready_reranker(scores: list[float] | None = None, **kwargs) -> Reranker:
    r = Reranker(**kwargs)
    r._model = _FakeCrossEncoder(scores)
    r._available = True
    return r


class TestSigmoid:
    def test_midpoint(self):
        assert _sigmoid(0.0) == 0.5

    def test_monotonic_and_bounded(self):
        vals = [_sigmoid(x) for x in (-100.0, -4.0, -1.0, 0.0, 1.0, 4.0, 100.0)]
        assert vals == sorted(vals)
        assert all(0.0 <= v <= 1.0 for v in vals)

    def test_no_overflow_at_extremes(self):
        assert _sigmoid(-1000.0) >= 0.0
        assert _sigmoid(1000.0) <= 1.0


class TestLocalScoreNormalization:
    def test_logits_normalized_to_unit_interval(self):
        r = _ready_reranker([-4.0, 0.0, 4.0])
        scores = r.rerank("查询", ["a", "b", "c"])
        assert scores is not None
        assert scores[1] == 0.5
        assert scores[0] < 0.1 < 0.9 < scores[2]

    def test_scores_are_comparable_to_api_scale(self):
        # 10 分以上的高相关 logit 不应溢出成 >1 的分数
        r = _ready_reranker([12.5])
        scores = r.rerank("查询", ["a"])
        assert scores is not None and 0.0 < scores[0] <= 1.0

    def test_not_ready_returns_none(self):
        r = Reranker()
        assert r.rerank("查询", ["a"]) is None

    def test_empty_texts_returns_none(self):
        r = _ready_reranker([1.0])
        assert r.rerank("查询", []) is None

    def test_predict_failure_returns_none(self):
        r = Reranker()
        r._model = _FakeCrossEncoder(raise_on_predict=True)
        r._available = True
        assert r.rerank("查询", ["a"]) is None


class TestSourceHandling:
    def test_local_init_delegates_to_model_hub(self, monkeypatch):
        calls: dict = {}

        def _fake_loader(model_name, source, device, max_length):
            calls.update(model_name=model_name, source=source, device=device, max_length=max_length)
            return _FakeCrossEncoder([2.0])

        monkeypatch.setattr(model_hub, "load_cross_encoder", _fake_loader)

        r = Reranker(
            model_name="BAAI/bge-reranker-base",
            device="cpu",
            download_source="modelscope",
        )
        r._do_init()

        assert r.available
        assert calls["source"] == "modelscope"
        assert calls["model_name"] == "BAAI/bge-reranker-base"
        assert r.rerank("查询", ["a"]) == [_sigmoid(2.0)]

    def test_init_failure_degrades_silently(self, monkeypatch):
        def _explode(*args, **kwargs):
            raise RuntimeError("下载失败")

        monkeypatch.setattr(model_hub, "load_cross_encoder", _explode)

        r = Reranker()
        r._do_init()  # 不应抛出
        assert r.available is False
        assert r.rerank("查询", ["a"]) is None

    def test_api_mode_does_not_load_model(self, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("API 模式下不应加载本地模型")

        monkeypatch.setattr(model_hub, "load_cross_encoder", _explode)

        r = Reranker(api_base="https://api.siliconflow.cn/v1", api_key="sk-x")
        r._do_init()
        assert r.available is True
        assert r.use_api is True
