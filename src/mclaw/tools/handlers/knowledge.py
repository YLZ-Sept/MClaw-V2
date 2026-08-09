"""
Knowledge Base 搜索处理器

通过知识库混合检索（BM25 + 向量 + RRF 融合）搜索已入库的文档内容。

# ApprovalClass checklist (新增 / 修改工具时必读)
# 1. 在本文件 Handler 类的 TOOLS 列表加新工具名
# 2. 在同 Handler 类的 TOOL_CLASSES 字典加 ApprovalClass 显式声明
# 3. 行为依赖参数 → 在 policy_v2/classifier.py:_refine_with_params 加分支
# 4. 跑 pytest tests/unit/test_classifier_completeness.py 验证
# 详见 docs/policy_v2_research.md §4.21
"""

import logging
from typing import TYPE_CHECKING, Any

from ...core.policy_v2 import ApprovalClass

if TYPE_CHECKING:
    from ...agent.core import Agent

logger = logging.getLogger(__name__)


class KnowledgeHandler:
    TOOLS = ["knowledge_search"]
    TOOL_CLASSES = {"knowledge_search": ApprovalClass.READONLY_SEARCH}

    def __init__(self, agent: "Agent"):
        self.agent = agent

    async def handle(self, tool_name: str, params: dict[str, Any]) -> str:
        if tool_name == "knowledge_search":
            return await self._knowledge_search(params)
        return f"❌ Unknown knowledge tool: {tool_name}"

    async def _knowledge_search(self, params: dict) -> str:
        query = params.get("query", "").strip()
        if not query:
            return "❌ knowledge_search 缺少必要参数 'query'。"

        collection_id = params.get("collection_id", "").strip() or None
        top_k = params.get("top_k", 5)
        top_k = max(1, min(20, top_k))

        # 检查 Profile 绑定的集合权限
        bound_collections: list[str] | None = None
        profile_id = getattr(self.agent, "_agent_profile_id", None)
        if profile_id:
            try:
                from mclaw.agents.profile import get_profile_store
                store = get_profile_store()
                profile = store.get(profile_id)
                if profile and profile.knowledge_collections:
                    bound_collections = profile.knowledge_collections
                    if collection_id and collection_id not in bound_collections:
                        return (
                            f"❌ 知识集合 '{collection_id}' 未绑定到此 Agent。"
                            f"绑定的集合: {', '.join(bound_collections) or '(无)'}"
                        )
            except Exception:
                pass

        try:
            from mclaw.api.routes.knowledge import get_knowledge_manager

            km = get_knowledge_manager()
            if km is None:
                return "❌ 知识库服务未初始化，请联系管理员。"

            # Must have at least one bound collection to search
            if not bound_collections:
                return "❌ 此 Agent 未绑定任何知识集合。请在 Agent 编辑中选择要使用的知识库。"

            if collection_id:
                if collection_id not in bound_collections:
                    return (
                        f"❌ 知识集合 '{collection_id}' 未绑定到此 Agent。"
                        f"绑定的集合: {', '.join(bound_collections)}"
                    )
                results = km.search_with_chunks(query, collection_id=collection_id, top_k=top_k)
            else:
                # Search all bound collections, merge results
                all_results: list = []
                for cid in bound_collections:
                    try:
                        batch = km.search_with_chunks(query, collection_id=cid, top_k=top_k)
                        all_results.extend(batch)
                    except Exception as e:
                        logger.warning(f"Knowledge search in collection {cid} failed: {e}")

                all_results.sort(key=lambda r: r.score, reverse=True)
                results = all_results[:top_k]

            if not results:
                scope = f"集合 {collection_id}" if collection_id else "知识库"
                return f"在{scope}中未找到与 '{query}' 匹配的结果。"

            return self._format_results(query, results)

        except ImportError:
            return "❌ 知识库模块不可用。"
        except Exception as e:
            logger.exception(f"knowledge_search failed: {e}")
            return f"❌ 搜索知识库时出错: {e}"

    @staticmethod
    def _format_results(query: str, results: list) -> str:
        lines = [f"知识库搜索结果: '{query}'", ""]
        for i, r in enumerate(results, 1):
            chunk = r.chunk
            meta = chunk.metadata or {}
            doc_name = meta.get("filename", "未知文档")
            lines.append(
                f"  [{i}] {doc_name} | "
                f"相关度: {r.score:.2f} | "
                f"来源: {r.source_type}"
            )
            content = chunk.content[:500] + "..." if len(chunk.content) > 500 else chunk.content
            lines.append(f"      {content.strip()}")
            lines.append("")
        return "\n".join(lines)


def create_handler(agent: "Agent"):
    handler = KnowledgeHandler(agent)
    return handler.handle
