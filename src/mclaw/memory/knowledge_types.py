"""
知识库数据类型

知识库（Knowledge Base）是 Mclaw 记忆系统的外部文档扩展，支持：
- 多 collections：按主题/项目组织文档
- 多格式文档：PDF、Markdown、Word、TXT、HTML、URL
- 混合检索：BM25（jieba 中文分词）+ 向量（ChromaDB）+ RRF 融合
- 优雅降级：ChromaDB 不可用时自动回退到纯 FTS5 模式
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _short_uuid() -> str:
    return str(uuid.uuid4())[:12]


class DocStatus(str, Enum):
    """文档索引状态"""
    PENDING = "pending"       # 待处理
    INDEXING = "indexing"     # 索引中
    READY = "ready"           # 就绪
    ERROR = "error"           # 索引失败


class DocFileType(str, Enum):
    """支持的文档格式"""
    PDF = "pdf"
    MARKDOWN = "md"
    TEXT = "txt"
    DOCX = "docx"
    HTML = "html"
    XLSX = "xlsx"
    XLS = "xls"
    CSV = "csv"
    URL = "url"


# 支持的文件扩展名映射
EXTENSION_MAP: dict[str, DocFileType] = {
    ".pdf": DocFileType.PDF,
    ".md": DocFileType.MARKDOWN,
    ".txt": DocFileType.TEXT,
    ".docx": DocFileType.DOCX,
    ".html": DocFileType.HTML,
    ".htm": DocFileType.HTML,
    ".xlsx": DocFileType.XLSX,
    ".xls": DocFileType.XLS,
    ".csv": DocFileType.CSV,
}

# 文件类型 → 人类可读标签
FILETYPE_LABELS: dict[DocFileType, str] = {
    DocFileType.PDF: "PDF",
    DocFileType.MARKDOWN: "Markdown",
    DocFileType.TEXT: "TXT",
    DocFileType.DOCX: "Word",
    DocFileType.HTML: "HTML",
    DocFileType.XLSX: "Excel",
    DocFileType.XLS: "Excel",
    DocFileType.CSV: "CSV",
    DocFileType.URL: "URL",
}


@dataclass
class KnowledgeCollection:
    """知识库 collection（类似文件夹/项目）"""
    id: str = field(default_factory=_short_uuid)
    name: str = ""
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    doc_count: int = 0
    chunk_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "doc_count": self.doc_count,
            "chunk_count": self.chunk_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgeCollection":
        return cls(
            id=d.get("id", _short_uuid()),
            name=d.get("name", ""),
            description=d.get("description", ""),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            doc_count=d.get("doc_count", 0),
            chunk_count=d.get("chunk_count", 0),
        )


@dataclass
class KnowledgeDocument:
    """知识库中的单个文档"""
    id: str = field(default_factory=_short_uuid)
    collection_id: str = ""
    filename: str = ""
    file_path: str | None = None
    file_type: str = ""        # DocFileType value
    file_size: int = 0
    checksum: str = ""         # SHA256
    status: str = DocStatus.PENDING.value
    chunk_count: int = 0
    error_message: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "filename": self.filename,
            "file_path": self.file_path,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "checksum": self.checksum,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgeDocument":
        return cls(
            id=d.get("id", _short_uuid()),
            collection_id=d.get("collection_id", ""),
            filename=d.get("filename", ""),
            file_path=d.get("file_path"),
            file_type=d.get("file_type", ""),
            file_size=d.get("file_size", 0),
            checksum=d.get("checksum", ""),
            status=d.get("status", DocStatus.PENDING.value),
            chunk_count=d.get("chunk_count", 0),
            error_message=d.get("error_message"),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            metadata=d.get("metadata", {}),
        )


@dataclass
class KnowledgeChunk:
    """文档分块"""
    id: str = field(default_factory=_short_uuid)
    doc_id: str = ""
    collection_id: str = ""
    content: str = ""
    chunk_index: int = 0
    token_count: int = 0
    embedding_id: str | None = None   # ChromaDB 中的向量 ID
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "collection_id": self.collection_id,
            "content": self.content,
            "chunk_index": self.chunk_index,
            "token_count": self.token_count,
            "embedding_id": self.embedding_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgeChunk":
        return cls(
            id=d.get("id", _short_uuid()),
            doc_id=d.get("doc_id", ""),
            collection_id=d.get("collection_id", ""),
            content=d.get("content", ""),
            chunk_index=d.get("chunk_index", 0),
            token_count=d.get("token_count", 0),
            embedding_id=d.get("embedding_id"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class SearchResult:
    """混合检索结果"""
    chunk: KnowledgeChunk
    score: float = 0.0           # RRF 融合后分数
    bm25_score: float = 0.0
    vector_score: float = 0.0
    source_type: str = "hybrid"  # bm25 / vector / hybrid

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_dict(),
            "score": self.score,
            "bm25_score": self.bm25_score,
            "vector_score": self.vector_score,
            "source_type": self.source_type,
        }


@dataclass
class CollectionStats:
    """Collection 统计信息"""
    collection_id: str = ""
    doc_count: int = 0
    chunk_count: int = 0
    total_chars: int = 0
    storage_bytes: int = 0
    indexed_count: int = 0     # 已向量化的 chunk 数
    error_count: int = 0       # 索引失败的文档数

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "doc_count": self.doc_count,
            "chunk_count": self.chunk_count,
            "total_chars": self.total_chars,
            "storage_bytes": self.storage_bytes,
            "indexed_count": self.indexed_count,
            "error_count": self.error_count,
        }
