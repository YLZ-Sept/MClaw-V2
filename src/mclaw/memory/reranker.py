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
import time

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
        api_style: str = "auto",
        timeout: float = 8.0,
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
        self.api_style = (api_style or "auto").strip().lower()
        self.timeout = float(timeout)
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

    def test_connection(self) -> dict:
        """用固定测试句探测在线端点，返回 ok/latency_ms/scores 或 error"""
        if not self.use_api:
            return {"ok": False, "error": "该端点未配置在线 API"}
        texts = ["这是一段与测试查询相关的文本。", "这是一段无关的文本。"]
        started = time.perf_counter()
        try:
            scores = self._rerank_api("测试查询", texts, raise_on_error=True)
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "scores": scores,
            }
        except Exception as e:
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": str(e),
            }

    def _rerank_api(
        self, query: str, texts: list[str], *, raise_on_error: bool = False
    ) -> list[float] | None:
        """在线精排：支持 SiliconFlow / Jina 风格与 DashScope 风格。

        - siliconflow/jina: POST {api_base}/rerank
        - dashscope: POST {api_base}，body 为 input/parameters 结构，
          api_base 需填完整服务地址
        """
        style = self.api_style
        if style == "auto":
            if "compatible-api" in self.api_base or "maas.aliyuncs.com" in self.api_base:
                style = "openai_rerank"  # 百炼 qwen3-rerank
            elif "dashscope" in self.api_base:
                style = "dashscope"
            else:
                style = "siliconflow"

        started = time.perf_counter()
        try:
            import httpx

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=self.timeout) as client:
                if style == "dashscope":
                    # 百炼原生接口：gte-rerank-v2 / qwen3.7-text-rerank。
                    # qwen3.7 不支持 return_documents，传了会被拒绝或忽略。
                    params: dict = {"top_n": len(texts)}
                    if "qwen3.7" not in self.api_model:
                        params["return_documents"] = False
                    resp = client.post(
                        self.api_base,
                        headers=headers,
                        json={
                            "model": self.api_model,
                            "input": {"query": query, "documents": texts},
                            "parameters": params,
                        },
                    )
                elif style in {"openai_rerank", "qwen", "bailian"}:
                    # 百炼 qwen3-rerank: OpenAI 兼容接口，路径是 /reranks（复数）
                    url = self.api_base.rstrip("/")
                    if not url.endswith("/reranks"):
                        url = f"{url}/reranks"
                    resp = client.post(
                        url,
                        headers=headers,
                        json={
                            "model": self.api_model,
                            "query": query,
                            "documents": texts,
                            "top_n": len(texts),
                        },
                    )
                else:
                    resp = client.post(
                        f"{self.api_base}/rerank",
                        headers=headers,
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

            if style == "dashscope":
                results = (data.get("output") or {}).get("results", []) or []
            else:
                results = data.get("results", []) or []

            scores = [0.0] * len(texts)
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", item.get("score", 0.0))
                if isinstance(idx, int) and 0 <= idx < len(texts):
                    scores[idx] = float(score)

            # 审计：只记录集合/条数/耗时，不记录正文
            logger.info(
                f"[Reranker] API 精排完成: style={style} candidates={len(texts)} "
                f"latency={time.perf_counter() - started:.3f}s"
            )
            return scores
        except Exception as e:
            logger.warning(
                f"[Reranker] API 精排失败（style={style}, "
                f"{time.perf_counter() - started:.3f}s），沿用 RRF 排序: {e}"
            )
            if raise_on_error:
                raise
            return None
