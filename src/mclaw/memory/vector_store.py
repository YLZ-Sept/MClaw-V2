"""
向量存储 - 基于 ChromaDB

提供语义搜索能力:
- 记忆向量化存储
- 语义相似度搜索
- 支持按类型过滤
- 支持多源下载 (HuggingFace / hf-mirror / ModelScope)
"""

import asyncio
import logging
import threading
from pathlib import Path

from .types import normalize_tags

logger = logging.getLogger(__name__)

# 延迟导入，避免未安装依赖时报错
_sentence_transformers_available = None
_chromadb = None


def _lazy_import():
    """延迟导入依赖"""
    global _sentence_transformers_available, _chromadb

    if _sentence_transformers_available is None:
        # 模块可能在服务运行期间安装，路径尚未注入 sys.path。
        # 在导入前尝试刷新一次（idempotent，不会重复添加已有路径）。
        import sys

        if "sentence_transformers" not in sys.modules:
            try:
                from mclaw.runtime_env import inject_module_paths_runtime

                inject_module_paths_runtime()
            except Exception:
                pass

        try:
            import sentence_transformers  # noqa: F401

            _sentence_transformers_available = True
        except ImportError as e:
            from mclaw.tools._import_helper import import_or_hint

            hint = import_or_hint("sentence_transformers")
            logger.info(f"[VectorStore] 向量搜索未启用: {hint}")
            logger.debug(f"sentence_transformers ImportError 详情: {e}", exc_info=True)
            _sentence_transformers_available = False
            return False

    if not _sentence_transformers_available:
        return False

    if _chromadb is None:
        try:
            # 在导入前禁用 chromadb 遥测，避免因 posthog 缺失导致 ImportError
            # chromadb 在 import 时会检查这些环境变量
            import os

            os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
            os.environ.setdefault("CHROMA_TELEMETRY", "False")

            import chromadb

            _chromadb = chromadb
        except ImportError as e:
            from mclaw.tools._import_helper import import_or_hint

            hint = import_or_hint("chromadb")
            logger.info(f"[VectorStore] ChromaDB 未启用: {hint}")
            logger.debug(f"chromadb ImportError 详情: {e}", exc_info=True)
            return False

    return True


def needs_vector_rebuild(
    metadata: dict | None, existing_count: int, model_name: str
) -> bool:
    """判断向量 collection 是否需要全量重建。

    向量维度由 embedding 模型决定（text2vec-base-chinese=768 维 →
    bge-small-zh-v1.5=512 维），指纹不一致的存量向量与新模型不兼容：查询
    会因维度不符抛错，被调用方吞掉后静默退化成"搜不到"。这类向量已经不可用，
    重建是唯一出路。

    三种重建情形：
      1. 指纹存在且与当前模型不同 —— 明确的模型切换
      2. 指纹缺失但集合非空 —— 旧版本（引入指纹之前）建的存量向量，模型未知
      3. reindex_pending —— 上次重建中途退出（进程被杀/重启），回来接着做
    指纹缺失且集合为空时无数据可重建，调用方补记指纹即可。
    """
    meta = metadata or {}
    existing_model = meta.get("embedding_model", "")
    switched_model = bool(existing_model) and existing_model != model_name
    legacy_vectors = bool(existing_count) and not existing_model
    return switched_model or legacy_vectors or bool(meta.get("reindex_pending"))


class VectorStore:
    """
    向量存储 - 基于 ChromaDB

    使用本地 embedding 模型，无需 API 调用。
    支持多下载源 (HuggingFace / hf-mirror / ModelScope)。

    初始化策略：
    - 模型下载在后台线程中进行，绝不阻塞后端启动
    - 下载完成前，所有操作优雅降级（返回空结果）
    - 下载失败后有冷却重试机制
    """

    # 默认使用中文优化的 embedding 模型（BGE，C-MTEB 中文检索精度优于 text2vec）
    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

    def __init__(
        self,
        data_dir: Path,
        model_name: str | None = None,
        device: str = "cpu",
        download_source: str = "auto",
    ):
        """
        初始化向量存储

        Args:
            data_dir: 数据目录
            model_name: embedding 模型名称 (默认 BAAI/bge-small-zh-v1.5)
            device: 设备 (cpu 或 cuda)
            download_source: 下载源 ("auto" | "huggingface" | "hf-mirror" | "modelscope")
        """
        self.data_dir = Path(data_dir)
        self.model_name = model_name or self.DEFAULT_MODEL
        self.device = device
        self.download_source = download_source

        self._model = None
        self._client = None
        self._collection = None
        self._enabled = False

        # embedding 模型切换（向量维度不兼容）后的重建状态
        self._needs_rebuild = False
        self._rebuild_started = False
        # 由 MemoryManager 注入：返回全部记忆 [{"id","content","type",...}]
        self._rebuild_provider = None

        # 初始化状态机
        self._init_state = "idle"  # idle → loading → ready / failed
        self._init_failed = False
        self._init_fail_time: float = 0.0
        self._init_retry_cooldown: float = 300.0  # 失败后 5 分钟冷却再重试
        self._retry_count: int = 0
        self._import_missing: bool = False  # 依赖缺失（ImportError）
        self._lock = threading.RLock()

        # 立即启动后台初始化（不阻塞调用方）
        self._start_background_init()

    def _start_background_init(self) -> None:
        """在后台线程中启动初始化，不阻塞调用方。"""
        t = threading.Thread(
            target=self._do_initialize,
            name="VectorStore-init",
            daemon=True,
        )
        t.start()
        logger.info("[VectorStore] 后台初始化已启动（不阻塞后端启动）")

    def _do_initialize(self) -> None:
        """实际执行初始化（在后台线程中运行）。"""
        with self._lock:
            if self._init_state == "loading":
                return  # 已有线程在初始化
            self._init_state = "loading"

        try:
            self._do_initialize_inner()
        except Exception:
            pass  # 错误已在 inner 中处理

    def _do_initialize_inner(self) -> None:
        """初始化核心逻辑，包含模型下载和 ChromaDB 初始化。"""
        import time as _time

        # ── 关键：在导入 sentence_transformers 之前就配置好 HF_ENDPOINT ──
        # sentence_transformers 导入时会触发 huggingface_hub 导入，
        # 而 huggingface_hub 在模块级缓存 HF_ENDPOINT。
        # 如果不提前设置，缓存值会是 https://huggingface.co，
        # 即使后续改了 os.environ 也不会生效。
        try:
            from .model_hub import _apply_source_env, _resolve_source

            resolved = _resolve_source(self.download_source)
            if resolved.value == "auto":
                from .model_hub import detect_best_source

                resolved = detect_best_source()
            _apply_source_env(resolved)
            logger.info(f"[VectorStore] 预配置 HF_ENDPOINT (源={resolved.value})")
        except Exception as e:
            logger.debug(f"[VectorStore] 预配置 HF_ENDPOINT 失败 (非致命): {e}")

        if not _lazy_import():
            with self._lock:
                self._enabled = False
                self._init_state = "failed"
                self._init_failed = True
                self._import_missing = True
                self._init_fail_time = _time.monotonic()
                self._retry_count += 1
            return

        try:
            # 初始化 embedding 模型（支持多源下载）
            from .model_hub import load_embedding_model

            logger.info(
                f"[VectorStore] 正在加载 embedding 模型: {self.model_name} "
                f"(source={self.download_source})"
            )
            model = load_embedding_model(
                model_name=self.model_name,
                source=self.download_source,
                device=self.device,
            )

            # 初始化 ChromaDB
            chromadb_dir = self.data_dir / "chromadb"
            chromadb_dir.mkdir(parents=True, exist_ok=True)

            from chromadb.config import Settings

            client = _chromadb.PersistentClient(
                path=str(chromadb_dir),
                settings=Settings(anonymized_telemetry=False),
            )

            # 获取或创建 collection（metadata 记录 embedding 模型指纹）
            collection = client.get_or_create_collection(
                name="memories",
                metadata={"hnsw:space": "cosine"},
            )

            # ── embedding 模型指纹检测 ──
            # 与 KnowledgeManager 同款问题：向量维度由模型决定
            # （text2vec=768 维 → bge-small=512 维），旧向量与新模型不兼容，
            # 混在一个 collection 里会让查询抛错并静默退化成"搜不到"。
            metadata = collection.metadata or {}
            existing_model = metadata.get("embedding_model", "")
            existing_count = collection.count()
            needs_rebuild = needs_vector_rebuild(metadata, existing_count, self.model_name)

            if needs_rebuild:
                logger.warning(
                    f"[VectorStore] 重建记忆向量: "
                    f"{existing_model or '(旧版无模型指纹)'} → {self.model_name}"
                    f"（存量向量 {existing_count} 条，维度不兼容）"
                )
                try:
                    client.delete_collection("memories")
                except Exception:
                    pass
                collection = client.get_or_create_collection(
                    name="memories",
                    metadata={
                        "hnsw:space": "cosine",
                        "embedding_model": self.model_name,
                        # 重建完成后清除；中途退出则下次启动重试
                        "reindex_pending": True,
                    },
                )
            elif not existing_model:
                try:
                    collection.modify(
                        metadata={**metadata, "embedding_model": self.model_name}
                    )
                except Exception as e:
                    logger.debug(f"[VectorStore] 写入 embedding 指纹失败: {e}")

            # 全部成功，原子性地设置状态
            with self._lock:
                self._model = model
                self._client = client
                self._collection = collection
                self._enabled = True
                self._init_state = "ready"
                self._init_failed = False
                self._import_missing = False
                self._retry_count = 0
                self._needs_rebuild = needs_rebuild

            logger.info(f"[VectorStore] ✓ 初始化完成，已加载 {collection.count()} 条记忆")

            if needs_rebuild:
                self._start_background_rebuild()

        except Exception as e:
            err_msg = str(e)
            if "posthog" in err_msg:
                logger.warning(
                    f"VectorStore 初始化失败（chromadb 遥测依赖缺失，不影响核心功能）: {e}"
                )
            elif "chromadb" in err_msg.lower():
                logger.warning(
                    f"VectorStore 初始化失败（chromadb 内部模块缺失，"
                    f"请尝试重新安装 vector-memory 模块）: {e}"
                )
            else:
                logger.error(f"[VectorStore] 初始化失败: {e}")

            with self._lock:
                self._enabled = False
                self._init_state = "failed"
                self._init_failed = True
                self._init_fail_time = _time.monotonic()
                self._retry_count += 1

    def _ensure_initialized(self) -> bool:
        """检查是否已初始化就绪。

        设计原则：**绝不阻塞调用方**。
        - 已就绪 → 返回 True
        - 正在加载 → 返回 False（调用方优雅降级）
        - 加载失败且冷却期已过 → 触发后台重试，返回 False
        - 依赖缺失（ImportError）→ 指数退避，最长 1 小时
        """
        global _sentence_transformers_available, _chromadb

        with self._lock:
            if self._init_state == "ready" and self._enabled:
                return True

            if self._init_state == "loading":
                return False

            if self._init_failed:
                import time as _time

                # 依赖缺失时指数退避：300s → 600s → 1200s → … → 3600s 封顶
                if self._import_missing:
                    cooldown = min(
                        self._init_retry_cooldown * (2 ** (self._retry_count - 1)),
                        3600.0,
                    )
                else:
                    cooldown = self._init_retry_cooldown

                elapsed = _time.monotonic() - self._init_fail_time
                if elapsed < cooldown:
                    return False
                logger.info(
                    f"[VectorStore] 上次初始化失败已过 {elapsed:.0f}s"
                    f"（重试 #{self._retry_count + 1}），后台重新尝试..."
                )
                self._init_failed = False
                # 重置全局导入缓存，允许重新尝试 import
                # （模块可能在冷却期内通过设置中心安装）
                _sentence_transformers_available = None
                _chromadb = None

        self._start_background_init()
        return False

    @property
    def enabled(self) -> bool:
        """是否可用"""
        return self._ensure_initialized()

    # ── embedding 模型切换后的重建 ────────────────────────────────────────────

    @property
    def needs_rebuild(self) -> bool:
        """本次初始化是否因模型切换清空了向量库（等待重建）"""
        with self._lock:
            return self._needs_rebuild

    def set_rebuild_provider(self, provider) -> None:
        """注入"返回全部记忆"的取数回调，并在需要时启动重建。

        VectorStore 自己不持有记忆正文（正文在 MemoryManager 的 SQLite 里），
        所以重建只能由外部喂数据。init 在后台线程跑，provider 可能在 init
        之前或之后才注入，两个入口都会尝试启动重建，由 _rebuild_started 去重。
        """
        self._rebuild_provider = provider
        if self.needs_rebuild:
            self._start_background_rebuild()

    def _start_background_rebuild(self) -> None:
        """后台线程把既有记忆重新灌入向量库（不阻塞检索）"""
        if self._rebuild_provider is None:
            # provider 还没注入：set_rebuild_provider 会接手
            return
        with self._lock:
            if self._rebuild_started:
                return
            self._rebuild_started = True

        t = threading.Thread(
            target=self._run_rebuild,
            name="VectorStore-rebuild",
            daemon=True,
        )
        t.start()

    def _run_rebuild(self) -> None:
        try:
            items = self._rebuild_provider() or []
            logger.info(f"[VectorStore] 开始重建记忆向量: {len(items)} 条")
            added = self.rebuild(items)
            logger.info(f"[VectorStore] 记忆向量重建完成: {added}/{len(items)} 条")
        except Exception as e:
            logger.error(f"[VectorStore] 记忆向量重建失败: {e}", exc_info=True)
        finally:
            with self._lock:
                self._rebuild_started = False

    def rebuild(self, memories: list[dict]) -> int:
        """按外部提供的记忆全量重嵌入；全部成功才清除 pending 标记"""
        if not memories:
            # 没有记忆可灌（全新库）：直接收尾，否则标志位会一直挂着 True
            with self._lock:
                self._needs_rebuild = False
            self._clear_reindex_pending()
            return 0

        added = self.batch_add(memories)
        if added == len(memories):
            self._clear_reindex_pending()
        else:
            logger.warning(
                f"[VectorStore] 记忆向量重建不完整 ({added}/{len(memories)})，"
                f"保留 pending 标记，下次启动重试"
            )
        with self._lock:
            self._needs_rebuild = added != len(memories)
        return added

    def _clear_reindex_pending(self) -> None:
        """重建成功后清除 collection 上的 pending 标记"""
        with self._lock:
            coll = self._collection
        if coll is None:
            return
        try:
            coll.modify(
                metadata={
                    **(coll.metadata or {}),
                    "embedding_model": self.model_name,
                    "reindex_pending": False,
                }
            )
        except Exception as e:
            logger.debug(f"[VectorStore] 清除 reindex_pending 失败: {e}")

    def add_memory(
        self,
        memory_id: str,
        content: str,
        memory_type: str,
        priority: str,
        importance: float,
        tags: list[str] = None,
    ) -> bool:
        """
        添加记忆到向量库

        Args:
            memory_id: 记忆 ID
            content: 记忆内容
            memory_type: 记忆类型 (fact/preference/skill/error/rule/context)
            priority: 优先级 (transient/short_term/long_term/permanent)
            importance: 重要性评分 (0-1)
            tags: 标签列表

        Returns:
            是否成功
        """
        if not self._ensure_initialized():
            return False

        try:
            embedding = self._model.encode(content).tolist()

            with self._lock:
                self._collection.add(
                    ids=[memory_id],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[
                        {
                            "type": memory_type,
                            "priority": priority,
                            "importance": importance,
                            "tags": ",".join(normalize_tags(tags)),
                        }
                    ],
                )

            logger.debug(f"Added memory to vector store: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to add memory to vector store: {e}")
            return False

    def search(
        self,
        query: str,
        limit: int = 10,
        filter_type: str | None = None,
        min_importance: float = 0.0,
    ) -> list[tuple[str, float]]:
        """
        语义搜索

        Args:
            query: 搜索查询
            limit: 返回数量
            filter_type: 过滤类型 (可选)
            min_importance: 最小重要性 (可选)

        Returns:
            [(memory_id, distance), ...] 距离越小越相似
        """
        if not self._ensure_initialized():
            return []

        try:
            query_embedding = self._model.encode(query).tolist()

            with self._lock:
                where = None
                if filter_type:
                    where = {"type": filter_type}

                results = self._collection.query(
                    query_embeddings=[query_embedding],
                    n_results=limit,
                    where=where,
                )

            if not results["ids"] or not results["ids"][0]:
                return []

            # 返回 (id, distance) 列表
            ids = results["ids"][0]
            distances = results["distances"][0] if results.get("distances") else [0] * len(ids)

            # 过滤低重要性
            if min_importance > 0 and results.get("metadatas"):
                filtered = []
                for i, (mid, dist) in enumerate(zip(ids, distances, strict=False)):
                    meta = results["metadatas"][0][i]
                    if meta.get("importance", 0) >= min_importance:
                        filtered.append((mid, dist))
                return filtered

            return list(zip(ids, distances, strict=False))

        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            return []

    async def async_search(
        self,
        query: str,
        limit: int = 10,
        filter_type: str | None = None,
        min_importance: float = 0.0,
    ) -> list[tuple[str, float]]:
        """
        异步语义搜索（将 CPU 密集的 encode 操作放到线程池，避免阻塞事件循环）

        参数和返回值与 search() 完全相同。
        """
        return await asyncio.to_thread(self.search, query, limit, filter_type, min_importance)

    def delete_memory(self, memory_id: str) -> bool:
        """
        删除记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            是否成功
        """
        if not self._ensure_initialized():
            return False

        try:
            with self._lock:
                self._collection.delete(ids=[memory_id])
            logger.debug(f"Deleted memory from vector store: {memory_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete memory: {e}")
            return False

    def update_memory(
        self,
        memory_id: str,
        content: str,
        memory_type: str,
        priority: str,
        importance: float,
        tags: list[str] = None,
    ) -> bool:
        """
        更新记忆

        Args:
            memory_id: 记忆 ID
            content: 新内容
            memory_type: 记忆类型
            priority: 优先级
            importance: 重要性
            tags: 标签

        Returns:
            是否成功
        """
        if not self._ensure_initialized():
            return False

        try:
            embedding = self._model.encode(content).tolist()

            with self._lock:
                self._collection.update(
                    ids=[memory_id],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[
                        {
                            "type": memory_type,
                            "priority": priority,
                            "importance": importance,
                            "tags": ",".join(normalize_tags(tags)),
                        }
                    ],
                )

            logger.debug(f"Updated memory in vector store: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to update memory: {e}")
            return False

    def get_stats(self) -> dict:
        """获取统计信息"""
        if not self._ensure_initialized():
            return {"enabled": False, "count": 0}

        with self._lock:
            return {
                "enabled": True,
                "count": self._collection.count(),
                "model": self.model_name,
                "device": self.device,
            }

    def clear(self) -> bool:
        """清空所有记忆"""
        if not self._ensure_initialized():
            return False

        try:
            with self._lock:
                # 删除并重新创建 collection
                self._client.delete_collection("memories")
                self._collection = self._client.get_or_create_collection(
                    name="memories",
                    metadata={"hnsw:space": "cosine"},
                )
            logger.info("Cleared all memories from vector store")
            return True
        except Exception as e:
            logger.error(f"Failed to clear vector store: {e}")
            return False

    def batch_add(
        self,
        memories: list[dict],
    ) -> int:
        """
        批量添加记忆

        Args:
            memories: [{"id": ..., "content": ..., "type": ..., "priority": ..., "importance": ..., "tags": ...}, ...]

        Returns:
            成功添加的数量
        """
        if not self._ensure_initialized():
            return 0

        if not memories:
            return 0

        try:
            contents = [m["content"] for m in memories]
            embeddings = self._model.encode(contents).tolist()

            ids = [m["id"] for m in memories]
            metadatas = [
                {
                    "type": m.get("type", "fact"),
                    "priority": m.get("priority", "short_term"),
                    "importance": m.get("importance", 0.5),
                    "tags": ",".join(normalize_tags(m.get("tags"))),
                }
                for m in memories
            ]

            with self._lock:
                self._collection.add(
                    ids=ids,
                    embeddings=embeddings,
                    documents=contents,
                    metadatas=metadatas,
                )

            logger.info(f"Batch added {len(memories)} memories to vector store")
            return len(memories)

        except Exception as e:
            logger.error(f"Batch add failed: {e}")
            return 0
