"""
知识库 API 路由

提供 HTTP API 用于：
- Collection CRUD
- 文档上传/导入/删除/重索引
- 混合搜索（BM25 + 向量 + RRF 融合）
- 统计信息

所有端点挂载在 /api/knowledge 下。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from mclaw.memory.knowledge_manager import KnowledgeManager
from mclaw.memory.knowledge_types import DocStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["知识库"])

# ── 请求/响应模型 ─────────────────────────────────────────────────────────────

class CreateCollectionRequest(BaseModel):
    name: str
    description: str = ""
    is_public: bool = False


class UpdateCollectionRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None


class ImportUrlRequest(BaseModel):
    url: str


class SearchRequest(BaseModel):
    query: str
    collection_id: str | None = None
    top_k: int = 10


class CollectionResponse(BaseModel):
    id: str
    name: str
    description: str
    workspace_id: str = "default"
    owner_id: str = ""
    is_public: bool = False
    created_at: float
    updated_at: float
    doc_count: int
    chunk_count: int


class DocResponse(BaseModel):
    id: str
    collection_id: str
    filename: str
    file_path: str | None
    file_type: str
    file_size: int
    checksum: str
    status: str
    chunk_count: int
    error_message: str | None
    uploaded_by: str = ""
    created_at: float
    updated_at: float
    metadata: dict[str, Any]


class ChunkResponse(BaseModel):
    id: str
    doc_id: str
    collection_id: str
    content: str
    chunk_index: int
    token_count: int
    embedding_id: str | None
    metadata: dict[str, Any]


class SearchResultResponse(BaseModel):
    chunk: ChunkResponse
    score: float
    bm25_score: float
    vector_score: float
    source_type: str


class StatsResponse(BaseModel):
    collection_id: str
    doc_count: int
    chunk_count: int
    total_chars: int
    storage_bytes: int
    indexed_count: int
    error_count: int


class DeleteResponse(BaseModel):
    ok: bool
    deleted_chunks: int = 0


class PaginatedCollections(BaseModel):
    items: list[Any]
    total: int


class PaginatedDocuments(BaseModel):
    items: list[Any]
    total: int


class ErrorResponse(BaseModel):
    detail: str


# ── 依赖注入 ───────────────────────────────────────────────────────────────────

# 全局单例（在 server.py 启动时创建）
_knowledge_manager: KnowledgeManager | None = None


def get_knowledge_manager() -> KnowledgeManager:
    global _knowledge_manager
    if _knowledge_manager is None:
        raise HTTPException(status_code=503, detail="KnowledgeManager 尚未初始化")
    return _knowledge_manager


def set_knowledge_manager(km: KnowledgeManager) -> None:
    global _knowledge_manager
    _knowledge_manager = km


# ── 权限辅助 ─────────────────────────────────────────────────────────────────

def _current_owner(request: Request) -> tuple[str, str]:
    """从请求中提取 (user_id, workspace_id)"""
    user_id = (
        request.headers.get("X-Mclaw-User")
        or getattr(request.state, "user_id", None)
        or "admin"
    )
    workspace_id = (
        request.headers.get("X-Mclaw-Workspace")
        or getattr(request.state, "workspace_id", None)
        or "default"
    )
    return user_id, workspace_id


def _is_admin(request: Request, user_id: str) -> bool:
    """检查用户是否为管理员"""
    try:
        wac = request.app.state.web_access_config
        return wac.is_admin(user_id) if wac else False
    except Exception:
        return False


def _check_collection_access(
    coll: dict | None,
    user_id: str,
    _is_admin_flag: bool,
    require_write: bool = False,
) -> None:
    """校验 collection 访问权限，无权限时抛 403/404"""
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection 不存在")
    owner = coll.get("owner_id", "")
    is_public = bool(coll.get("is_public", 0))

    if _is_admin_flag or owner == user_id:
        return  # owner + admin 全权限
    if require_write:
        raise HTTPException(status_code=403, detail="无权修改此知识库")
    if not is_public:
        raise HTTPException(status_code=404, detail="Collection 不存在")


def _require_collection_exists(coll) -> None:
    """知识库为公共资源：只校验 collection 存在，不再做 owner/admin 隔离。"""
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection 不存在")


# ── Collection 路由 ────────────────────────────────────────────────────────────

@router.get("/collections")
async def list_collections(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出当前工作区的 collections（分页）"""
    km = get_knowledge_manager()
    user_id, workspace_id = _current_owner(request)
    items_list, total = km.list_collections(workspace_id, offset=offset, limit=limit)
    return {"items": [c.to_dict() for c in items_list], "total": total}


@router.post("/collections", response_model=CollectionResponse, status_code=201)
async def create_collection(request: Request, body: CreateCollectionRequest):
    """创建新的 collection"""
    km = get_knowledge_manager()
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Collection 名称不能为空")
    user_id, workspace_id = _current_owner(request)
    coll = km.create_collection(
        body.name.strip(), body.description.strip(),
        workspace_id=workspace_id, owner_id=user_id,
        is_public=body.is_public,
    )
    return coll.to_dict()


@router.get("/collections/{collection_id}", response_model=CollectionResponse)
async def get_collection(request: Request, collection_id: str):
    """获取 collection 详情"""
    km = get_knowledge_manager()
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)
    return coll.to_dict()


@router.put("/collections/{collection_id}", response_model=CollectionResponse)
async def update_collection(request: Request, collection_id: str, body: UpdateCollectionRequest):
    """更新 collection"""
    km = get_knowledge_manager()
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)
    ok = km.update_collection(collection_id, body.name, body.description)
    if not ok and body.is_public is None:
        raise HTTPException(status_code=400, detail="无更新内容")
    # 更新 is_public
    if body.is_public is not None:
        conn = km._get_conn()
        conn.execute("UPDATE kb_collections SET is_public = ? WHERE id = ?",
                     (int(body.is_public), collection_id))
        conn.commit()
        conn.close()
    coll = km.get_collection(collection_id)
    if coll is None:
        raise HTTPException(status_code=404, detail="Collection 不存在")
    return coll.to_dict()


@router.delete("/collections/{collection_id}", response_model=DeleteResponse)
async def delete_collection(request: Request, collection_id: str):
    """删除 collection 及其所有文档、chunks、向量"""
    km = get_knowledge_manager()
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)
    deleted = km.delete_collection(collection_id)
    return DeleteResponse(ok=True, deleted_chunks=deleted)


@router.get("/collections/{collection_id}/stats", response_model=StatsResponse)
async def get_collection_stats(collection_id: str):
    """获取 collection 统计信息"""
    km = get_knowledge_manager()
    if km.get_collection(collection_id) is None:
        raise HTTPException(status_code=404, detail="Collection 不存在")
    return km.get_stats(collection_id).to_dict()


# ── Document 路由 ──────────────────────────────────────────────────────────────

@router.get("/collections/{collection_id}/documents")
async def list_documents(
    request: Request, collection_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出 collection 中的文档（分页）"""
    km = get_knowledge_manager()
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)
    items_list, total = km.list_documents(collection_id, offset=offset, limit=limit)
    return {"items": [d.to_dict() for d in items_list], "total": total}


@router.post("/collections/{collection_id}/upload", response_model=DocResponse, status_code=201)
async def upload_document(request: Request, collection_id: str, file: UploadFile = File(...)):
    """上传文档到 collection"""
    km = get_knowledge_manager()
    user_id, _ws = _current_owner(request)
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)

    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    suffix = Path(file.filename).suffix.lower()
    from mclaw.memory.knowledge_types import EXTENSION_MAP
    if suffix not in EXTENSION_MAP:
        allowed = ", ".join(EXTENSION_MAP.keys())
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {suffix}，支持: {allowed}",
        )

    upload_dir = km.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 文件夹上传时文件名可能含子目录路径，用 _ 替换防止路径穿越
    safe_name = file.filename.replace("\\", "_").replace("/", "_")
    tmp_path = upload_dir / f"{int(time.time())}_{safe_name}"

    try:
        content = await file.read()
        tmp_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    try:
        doc = km.ingest_file(collection_id, tmp_path, uploaded_by=user_id)
    except ValueError as e:
        # 空内容等业务错误：保留文件，用户可下载后自行处理
        logger.warning(f"文档摄入失败（保留文件）: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"文档摄入失败: {e}", exc_info=True)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"文档索引失败: {e}")

    return doc.to_dict()


@router.post("/collections/{collection_id}/import-url", response_model=DocResponse, status_code=201)
async def import_url(request: Request, collection_id: str, body: ImportUrlRequest):
    """从 URL 导入网页内容"""
    km = get_knowledge_manager()
    user_id, _ws = _current_owner(request)
    coll = km.get_collection(collection_id)
    _require_collection_exists(coll)

    if not body.url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL 必须以 http:// 或 https:// 开头")

    try:
        doc = km.ingest_url(collection_id, body.url.strip(), uploaded_by=user_id)
    except Exception as e:
        logger.error(f"URL 导入失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"URL 导入失败: {e}")

    return doc.to_dict()


@router.get("/documents/{doc_id}", response_model=DocResponse)
async def get_document(doc_id: str):
    """获取文档详情"""
    km = get_knowledge_manager()
    doc = km.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc.to_dict()


@router.get("/documents/{doc_id}/chunks", response_model=list[ChunkResponse])
async def list_document_chunks(doc_id: str):
    """获取文档的 chunk 列表"""
    km = get_knowledge_manager()
    doc = km.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return [c.to_dict() for c in km.get_document_chunks(doc_id)]


@router.delete("/documents/{doc_id}", response_model=DeleteResponse)
async def delete_document(request: Request, doc_id: str):
    """删除文档及其 chunks 和向量（集合 owner 或管理员可删）"""
    km = get_knowledge_manager()
    doc = km.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    coll = km.get_collection(doc.collection_id)
    _require_collection_exists(coll)

    deleted = km.delete_document(doc_id)
    return DeleteResponse(ok=True, deleted_chunks=deleted)


@router.post("/documents/{doc_id}/reindex", response_model=DocResponse)
async def reindex_document(request: Request, doc_id: str):
    """重新索引文档"""
    km = get_knowledge_manager()
    doc = km.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    coll = km.get_collection(doc.collection_id)
    _require_collection_exists(coll)

    try:
        doc = km.reindex_document(doc_id)
    except Exception as e:
        logger.error(f"重索引失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重索引失败: {e}")

    return doc.to_dict()


# ── Search 路由 ────────────────────────────────────────────────────────────────

@router.post("/search", response_model=list[SearchResultResponse])
async def search_knowledge(request: Request, body: SearchRequest):
    """混合搜索知识库（BM25 + 向量 + RRF 融合）"""
    km = get_knowledge_manager()

    if not body.query.strip():
        raise HTTPException(status_code=400, detail="搜索查询不能为空")

    if body.top_k < 1 or body.top_k > 100:
        raise HTTPException(status_code=400, detail="top_k 必须在 1-100 之间")

    # 如果指定了 collection，校验存在性
    if body.collection_id:
        coll = km.get_collection(body.collection_id)
        _require_collection_exists(coll)

    results = km.search_with_chunks(
        query=body.query.strip(),
        collection_id=body.collection_id,
        top_k=body.top_k,
    )

    return [r.to_dict() for r in results]
