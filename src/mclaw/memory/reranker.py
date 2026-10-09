"""
Reranker 精排层 - 知识库检索二次排序

在 KnowledgeManager 的 BM25 + 向量 RRF 融合之后，对候选 chunks 用
cross-encoder 逐对精细打分，显著提升 top-k 准确率。

两种可插拔的后端：
- 本地 CrossEncoder（默认）：sentence_transformers.CrossEncoder + BAAI/bge-reranker-base，
  复用 model_hub 的多源下载（hf-mirror / ModelScope），后台线程加载
- 在线 Rerank API（可选）：兼容 SiliconFlow / Jina 风格的 POST /rerank 接口，
  设置 api_base + api_key 后优先于本地模型

设计约束（与 VectorStore 一致的优雅降级哲学）：
- 初始化在后台线程，绝不阻塞调用方
- 加载完成前 rerank() 返回 None，调用方直接沿用 RRF 排序
- 任何失败只记日志，不影响主检索链路

Usage:
    reranker = Reranker(model_name="BAAI/bge-reranker-base")
    reranker.start_background_init()
    scores = reranker.rerank("查询", ["候选1", "候选2"])   # None 或 [0.9, 0.1]
"""

from __future__ import annotations

import logging
import math
import threading

logger = logging.getLogger(__name__)


def _sigmoid(x: float) -> float:
    """数值稳定的 sigmoid（避免 exp 溢出）"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Reranker:
    """Cross-encoder 精排器（本地模型或在线 API，二选一）"""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        device: str = "cpu",
        download_source: str = "auto",
        api_base: str = "",
        api_key: str = "",
        api_model: str = "",
        max_length: int = 512,
    ):
        self.model_name = model_name
        self.device = device
        self.download_source = download_source
        self.max_length = max_length

        # API 模式：设置了 api_base + api_key 即走在线接口，否则本地模型
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.api_model = api_model or "BAAI/bge-reranker-v2-m3"
        self.use_api = bool(self.api_base and self.api_key)

        self._model = None          # 本地 CrossEncoder
        self._available = False
        self._init_started = False
        self._lock = threading.RLock()

    # ── 初始化 ─────────────────────────────────────────────────────────────

    def start_background_init(self) -> None:
        """后台线程初始化（不阻塞，失败静默降级）"""
        with self._lock:
            if self._init_started:
                return
            self._init_started = True

        t = threading.Thread(
            target=self._do_init,
            name="KnowledgeReranker-init",
            daemon=True,
        )
        t.start()

    def _do_init(self) -> None:
        if self.use_api:
            # API 模式无需加载模型，直接可用
            with self._lock:
                self._available = True
            logger.info(
                f"[Reranker] 在线 Rerank API 就绪: {self.api_base} (model={self.api_model})"
            )
            return

        try:
            # 复用 model_hub 的多源加载：ModelScope 需要走 snapshot_download +
            # 名称映射，直连 huggingface.co 在"HF 不通才选 ModelScope"的机器上必然失败。
            from .model_hub import load_cross_encoder

            logger.info(
                f"[Reranker] 加载本地 reranker 模型: {self.model_name} "
                f"(源={self.download_source}, 设备={self.device})"
            )
            model = load_cross_encoder(
                model_name=self.model_name,
                source=self.download_source,
                device=self.device,
                max_length=self.max_length,
            )
            with self._lock:
                self._model = model
                self._available = True
            logger.info(f"[Reranker] 本地 reranker 就绪: {self.model_name}")
        except Exception as e:
            logger.warning(
                f"[Reranker] 初始化失败，检索将沿用 RRF 排序（非致命）: {e}",
            )

    # ── 状态 ───────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        with self._lock:
            return self._available

    # ── 精排 ───────────────────────────────────────────────────────────────

    def rerank(self, query: str, texts: list[str]) -> list[float] | None:
        """
        对候选文本逐对打分。

        Returns:
            与 texts 对齐的相关性分数列表（越高越相关）；
            未就绪或失败时返回 None（调用方沿用原排序）。
        """
        if not self.available or not texts:
            return None

        if self.use_api:
            return self._rerank_api(query, texts)
        return self._rerank_local(query, texts)

    def _rerank_local(self, query: str, texts: list[str]) -> list[float] | None:
        try:
            with self._lock:
                model = self._model
            if model is None:
                return None
            pairs = [(query, t) for t in texts]
            scores = model.predict(pairs, show_progress_bar=False)
            # bge-reranker 系列输出的是原始 logit（无激活函数）。直接透出会出现
            # "相关度: -4.21" 这种分数——既没法给 LLM 读，也和在线 API 的
            # 0-1 relevance_score 不可比。统一 sigmoid 归一到 (0, 1)。
            return [_sigmoid(float(s)) for s in scores]
        except Exception as e:
            logger.warning(f"[Reranker] 本地精排失败，沿用 RRF 排序: {e}")
            return None

    def _rerank_api(self, query: str, texts: list[str]) -> list[float] | None:
        """SiliconFlow / Jina 风格 POST {api_base}/rerank"""
        try:
            import httpx

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self.api_base}/rerank",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.api_model,
                        "query": query,
                        "documents": texts,
                        "top_n": len(texts),
                        "return_documents": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # 标准返回: {"results": [{"index": 0, "relevance_score": 0.97}, ...]}
            results = data.get("results", [])
            scores = [0.0] * len(texts)
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", item.get("score", 0.0))
                if isinstance(idx, int) and 0 <= idx < len(texts):
                    scores[idx] = float(score)
            return scores
        except Exception as e:
            logger.warning(f"[Reranker] API 精排失败，沿用 RRF 排序: {e}")
            return None
