"""
知识库管理器（KnowledgeManager）

核心服务类，管理文档的完整生命周期：
- Collection 管理（创建/删除/列表）
- 文档摄入（解析 → 分块 → 嵌入 → 索引）
- 混合检索（BM25 + 向量 + RRF 融合）
- 优雅降级（ChromaDB 不可用时回退到纯 FTS5）

依赖：
- 复用 VectorStore 的 ChromaDB PersistentClient（独立 collection "knowledge"）
- 复用 model_hub 的 embedding 模型加载
- FTS5 + jieba 中文分词（零额外依赖）

Usage:
    manager = KnowledgeManager(data_dir=Path("data/knowledge"))
    coll = await manager.create_collection("技术文档", "项目相关技术资料")
    doc = await manager.ingest_file(coll.id, Path("doc.pdf"))
    results = await manager.search("如何使用", collection_id=coll.id, top_k=10)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .knowledge_types import (
    CollectionStats,
    DocFileType,
    DocStatus,
    EXTENSION_MAP,
    KnowledgeCollection,
    KnowledgeChunk,
    KnowledgeDocument,
    SearchResult,
    _short_uuid,
)
from .vector_store import needs_vector_rebuild

logger = logging.getLogger(__name__)

# ── 延迟导入 ──────────────────────────────────────────────────────────────────
_chromadb = None
_sentence_transformers_available = False


def _lazy_import_chroma() -> bool:
    global _chromadb, _sentence_transformers_available
    if _chromadb is not None:
        return True
    try:
        from chromadb.config import Settings  # noqa: F401
        import chromadb as _c
        _chromadb = _c
        _sentence_transformers_available = True
        return True
    except ImportError:
        logger.info("[KnowledgeManager] ChromaDB 未安装，将使用纯 FTS5 模式")
        return False


# ── 文本提取器 ─────────────────────────────────────────────────────────────────

def _pdf_ocr_fallback(file_path: Path) -> str:
    """图片型 PDF 的 OCR 回退：pdf2image → pytesseract / paddleocr"""
    ocr_parts: list[str] = []

    # 尝试 pdf2image + pytesseract
    try:
        from pdf2image import convert_from_path
        import pytesseract
        from PIL import Image

        images = convert_from_path(file_path, dpi=200)
        for i, img in enumerate(images):
            t = pytesseract.image_to_string(img, lang="chi_sim+eng")
            if t and t.strip():
                ocr_parts.append(t.strip())
        if ocr_parts:
            logger.info(f"[OCR] pytesseract 从 {len(images)} 页中提取了 {len(ocr_parts)} 页文字")
            return "\n\n".join(ocr_parts)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"[OCR] pytesseract 失败: {e}")

    # 回退: paddleocr（纯 Python，无需系统依赖）
    try:
        from paddleocr import PaddleOCR
        from pdf2image import convert_from_path

        ocr = PaddleOCR(lang="ch", use_angle_cls=True, show_log=False)
        images = convert_from_path(file_path, dpi=200)
        for img in images:
            import numpy as np
            result = ocr.ocr(np.array(img), cls=True)
            if result and result[0]:
                lines = [line[1][0] for line in result[0] if line[1][0]]
                if lines:
                    ocr_parts.append("\n".join(lines))
        if ocr_parts:
            logger.info(f"[OCR] PaddleOCR 从 {len(images)} 页中提取了 {len(ocr_parts)} 页文字")
            return "\n\n".join(ocr_parts)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"[OCR] PaddleOCR 失败: {e}")

    return ""


def _extract_doc_text(file_path: Path) -> str:
    """从旧版 .doc (Word 97-2003) 文件中提取文本"""
    # 方法 1: olefile 解析 OLE 二进制格式
    try:
        import olefile
        import struct

        ole = olefile.OleFileIO(str(file_path))
        if not ole.exists("WordDocument"):
            raise ValueError("不是有效的 .doc 文件：缺少 WordDocument 流")

        word_stream = ole.openstream("WordDocument").read()

        # 读取 FIB (File Information Block) 确定文本编码和位置
        # https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/cc3f69a5
        flags = struct.unpack_from("<H", word_stream, 0x000A)[0]
        # MS-DOC §2.5.1: FIB.fComplex 是位 0x0004（fWhichTblStm 才是 0x0200）。
        # 旧代码误用 0x0200，导致 fComplex 恒为 False，复杂文档走 fcMin/fcMac 直读
        # 仍可拿到大部分文本，但会漏掉 piece table 才有的字符合并/乱序段落。
        fComplex = bool(flags & 0x0004)

        # 主路径：fcMin/fcMac 直读（简单格式必然正确；复杂格式的 fcMin/fcMac
        # 同样覆盖主文本区）。WPS 生成的 .doc 在 piece table 上不兼容标准，
        # 直读是更稳妥的兜底。
        fcMin = struct.unpack_from("<I", word_stream, 0x0018)[0]
        fcMac = struct.unpack_from("<I", word_stream, 0x001C)[0]
        if fcMac > fcMin and fcMac <= len(word_stream):
            main_text = word_stream[fcMin:fcMac].decode("utf-16-le", errors="replace")
            # 质量检查：可读字符占比（中文/ASCII/常用标点/换行）
            import re as _re
            readable = _re.findall(r"[\u4e00-\u9fff\uff00-\uffef\x20-\x7e\r\n\t]", main_text)
            ratio = len(readable) / max(len(main_text), 1)
            if ratio > 0.7:
                ole.close()
                return main_text.replace("\x07", "\t").strip()

        if fComplex:
            # 复杂格式：文本存在 Table 流中
            # fcClx: file offset of Clx in table stream
            fcClx = struct.unpack_from("<I", word_stream, 0x01A2)[0]
            lcbClx = struct.unpack_from("<I", word_stream, 0x01A6)[0]

            table_stream_name = "1Table" if ole.exists("1Table") else "0Table"
            if ole.exists(table_stream_name):
                table_stream = ole.openstream(table_stream_name).read()
                clx = table_stream[fcClx:fcClx + lcbClx]

                # 解析 Clx：Prc (0x01) 携带 PlcPcd（piece table），
                # Pcdt (0x02) 是字符属性描述，跳过。
                # PlcPcd 布局: (n+1) 个 CP + n 个 PCD(8B)，n = (len-4)//12
                pos = 0
                prc = None
                while pos < len(clx):
                    entry_type = clx[pos]
                    if entry_type == 0x01:  # Prc
                        cb = struct.unpack_from("<H", clx, pos + 1)[0]
                        prc = clx[pos + 3:pos + 3 + cb]
                        pos += 3 + cb
                    elif entry_type == 0x02:  # Pcdt
                        _lcb = struct.unpack_from("<I", clx, pos + 1)[0]
                        pos += 5 + _lcb
                    else:
                        break

                if prc and len(prc) > 4:
                    n_pieces = (len(prc) - 4) // 12
                    cps = struct.unpack_from("<%dI" % (n_pieces + 1), prc, 0)
                    data_stream = ole.openstream("Data").read() if ole.exists("Data") else b""
                    parts: list[str] = []
                    for i in range(n_pieces):
                        pcd_off = (n_pieces + 1) * 4 + i * 8
                        fc_raw = struct.unpack_from("<I", prc, pcd_off)[0]
                        compressed = bool(fc_raw & 0x40000000)
                        fc = fc_raw & 0x3FFFFFFF
                        nchars = cps[i + 1] - cps[i]
                        if compressed:
                            # 8-bit ANSI (cp1252)，位于 Data 流
                            parts.append(data_stream[fc:fc + nchars].decode("cp1252", errors="replace"))
                        else:
                            # 16-bit UTF-16LE，位于 WordDocument 流
                            parts.append(word_stream[fc:fc + nchars * 2].decode("utf-16-le", errors="replace"))
                    ole.close()
                    if parts:
                        return "".join(parts).replace("\x07", "\t").strip()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"[DOC] olefile 解析失败: {e}")

    # 方法 2: python-docx 对 .doc 不支持，直接尝试读裸字节
    try:
        raw = file_path.read_bytes()
        # 尝试 UTF-16LE
        try:
            text = raw.decode("utf-16-le", errors="ignore")
            # 过滤掉过多的不可打印字符
            printable = "".join(c for c in text if c.isprintable() or c in "\n\r\t")
            if len(printable) > 100:
                return printable.strip()
        except Exception:
            pass
        # 尝试 ASCII / Latin-1
        text = raw.decode("latin-1", errors="ignore")
        import re
        words = re.findall(r"[\x20-\x7E一-鿿　-〿＀-￯]{3,}", text)
        if words:
            return " ".join(words)
    except Exception as e:
        logger.warning(f"[DOC] 原始读取失败: {e}")

    raise ImportError(
        "无法解析 .doc 文件。请安装 olefile 并将文件转为 .docx 格式后重试: "
        "pip install olefile"
    )


def _read_text_file(file_path: Path) -> str:
    """按 BOM → utf-8 → gb18030 的顺序读取纯文本文件。

    中文 Windows 上 Excel 导出的 .csv 默认是 GBK（"Unicode 文本"则是 UTF-16），
    中文网页另存的 .html 常是 GB2312/GBK。一律按 utf-8 读会整篇变成替换字符，
    且**静默入库**——检索不到还不报错。gb18030 是 GBK/GB2312 的超集，一个编码
    覆盖全部国标变体。

    顺序不能反：UTF-8 文本在 gb18030 下几乎总能"解码成功"（变成乱码），
    所以必须先试 utf-8；utf-8-sig 顺带吃掉 UTF-8 BOM（否则 \\ufeff 会混进正文）。
    """
    raw = file_path.read_bytes()
    # BOM 明确的情况：UTF-16（Windows 记事本 / Excel「Unicode 文本」常有）
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning(
        "[KnowledgeManager] %s 既不是 utf-8 也不是 gb18030，按 utf-8 容错解码"
        "（正文可能含替换字符）",
        file_path.name,
    )
    return raw.decode("utf-8", errors="replace")


def _extract_text(file_path: Path, file_type: DocFileType) -> str:
    """从文件中提取纯文本"""
    if file_type == DocFileType.TEXT or file_type == DocFileType.MARKDOWN:
        return _read_text_file(file_path)

    if file_type == DocFileType.PDF:
        text = ""
        # 第 1 步: pdfplumber（文字型 PDF）
        try:
            import pdfplumber
            text_parts: list[str] = []
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            text = "\n\n".join(text_parts)
        except ImportError:
            # fallback to PyPDF2
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(str(file_path))
                text = "\n\n".join(
                    page.extract_text() or "" for page in reader.pages
                )
            except ImportError:
                raise ImportError(
                    "PDF 解析需要安装 pdfplumber 或 PyPDF2: "
                    "pip install pdfplumber"
                )

        # 第 2 步: 文字提取为空 → 可能是图片型 PDF，尝试 OCR
        if not text.strip():
            ocr_text = _pdf_ocr_fallback(file_path)
            if ocr_text:
                return ocr_text

        return text

    if file_type == DocFileType.DOCX:
        try:
            from docx import Document
            doc = Document(str(file_path))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            raise ImportError(
                "DOCX 解析需要安装 python-docx: pip install python-docx"
            )

    if file_type == DocFileType.DOC:
        return _extract_doc_text(file_path)

    if file_type == DocFileType.HTML:
        try:
            from bs4 import BeautifulSoup

            # 传 bytes 而不是 str：BS4 会按 <meta charset> / BOM 自己嗅探编码，
            # 比在外部猜更准（中文网页另存下来常是 GB2312/GBK，且声明与实际
            # 编码未必一致）。外面先 decode 成 str 等于把这个能力废掉。
            soup = BeautifulSoup(file_path.read_bytes(), "html.parser")
            # Remove script/style tags
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            # bare fallback: strip tags naively；没有 BS4 的编码嗅探，
            # 退回到 BOM → utf-8 → gb18030 的通用顺序。
            import re
            html = _read_text_file(file_path)
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "", text)
            return text.strip()

    if file_type in (DocFileType.XLSX, DocFileType.XLS):
        return _extract_excel(file_path, file_type)

    if file_type in (DocFileType.PPTX, DocFileType.PPT):
        return _extract_pptx(file_path, file_type)

    if file_type == DocFileType.CSV:
        return _read_text_file(file_path)

    raise ValueError(f"不支持的文件类型: {file_type}")


def _extract_excel(file_path: Path, file_type: DocFileType) -> str:
    """从 Excel 文件提取文本（每个单元格一行，sheet 间加分隔）。"""
    if file_type == DocFileType.XLSX:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            parts: list[str] = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                parts.append(f"=== Sheet: {sheet_name} ===")
                for row in ws.iter_rows(values_only=True):
                    row_text = "\t".join(str(c) if c is not None else "" for c in row)
                    if row_text.strip():
                        parts.append(row_text)
            wb.close()
            return "\n".join(parts)
        except ImportError:
            raise ImportError(
                "Excel(.xlsx) 解析需要安装 openpyxl: pip install openpyxl"
            )
    else:
        # .xls — older format
        try:
            import xlrd
            wb = xlrd.open_workbook(str(file_path))
            parts: list[str] = []
            for sheet_name in wb.sheet_names():
                ws = wb.sheet_by_name(sheet_name)
                parts.append(f"=== Sheet: {sheet_name} ===")
                for row_idx in range(ws.nrows):
                    row_text = "\t".join(
                        str(ws.cell_value(row_idx, col_idx))
                        for col_idx in range(ws.ncols)
                    )
                    if row_text.strip():
                        parts.append(row_text)
            return "\n".join(parts)
        except ImportError:
            raise ImportError(
                "Excel(.xls) 解析需要安装 xlrd: pip install xlrd"
            )


def _extract_pptx(file_path: Path, file_type: DocFileType) -> str:
    """从 PowerPoint 文件提取文本（每页一个 section，含文本框与表格）。"""
    if file_type == DocFileType.PPT:
        raise ImportError(".ppt 旧格式暂不支持，请转为 .pptx 后重试")

    try:
        from pptx import Presentation

        prs = Presentation(str(file_path))
        parts: list[str] = []
        for i, slide in enumerate(prs.slides, 1):
            parts.append(f"=== 第 {i} 页 ===")
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    for para in shape.text_frame.paragraphs:
                        text = "".join(run.text for run in para.runs)
                        if text.strip():
                            parts.append(text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        parts.append("\t".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    except ImportError:
        raise ImportError(
            "PPTX 解析需要安装 python-pptx: pip install python-pptx"
        )


def _extract_text_from_url(url: str) -> str:
    """从 URL 下载并提取文本（尝试 requests + readability）"""
    import re

    try:
        import requests
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        html = resp.text
        # If encoding is not set, try to detect
        if resp.encoding and resp.encoding.lower() != "utf-8":
            html = resp.content.decode(resp.encoding, errors="replace")
    except ImportError:
        raise ImportError("URL 导入需要安装 requests: pip install requests")

    # Try readability-lxml for main content extraction
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title()
        content_html = doc.summary()
        # Strip tags from summary
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content_html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        return f"# {title}\n\n{text}" if title else text
    except ImportError:
        pass

    # Fallback: BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()


# ── 分块器 ────────────────────────────────────────────────────────────────────

def _chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 128,
) -> list[str]:
    """滑动窗口分块，按段落优先分割"""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            # If a single paragraph exceeds chunk_size, split by sentence
            if len(para) > chunk_size:
                sentences = _split_sentences(para)
                buf = ""
                for s in sentences:
                    if len(buf) + len(s) <= chunk_size:
                        buf = f"{buf}{s}" if buf else s
                    else:
                        if buf:
                            chunks.append(buf)
                        buf = s
                if buf:
                    current = buf
                else:
                    current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    # Apply overlap: each chunk (except first) prepends overlap chars from previous
    if chunk_overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            if len(prev) > chunk_overlap:
                overlapped.append(prev[-chunk_overlap:] + "\n\n" + chunks[i])
            else:
                overlapped.append(chunks[i])
        chunks = overlapped

    # 丢弃无有效检索字符的 chunk（纯 markdown 表格边框线、分隔符、空白）。
    # 实测某份周报 15 个 chunk 里有 3 个是这种，检索命中它们等于白占名额。
    # 放在这里而不是 _persist_chunks：所有调用方拿到的就是干净列表，
    # _mark_doc_ready(doc_id, len(chunks)) 的 chunk_count 才不会算多。
    return [c for c in chunks if not _is_degenerate_chunk(c)]


# 至少含一个汉字/字母/数字才算有效内容
_CONTENT_CHAR_RE = re.compile(r"[0-9A-Za-z一-鿿]")


def _is_degenerate_chunk(text: str) -> bool:
    """chunk 是否不含任何有效检索字符（只有空白、表格边框、分隔符）"""
    return _CONTENT_CHAR_RE.search(text) is None


def _split_sentences(text: str) -> list[str]:
    """中英文分句"""
    import re
    # Split on Chinese/English sentence endings
    parts = re.split(r"(?<=[。！？.!?\n])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _estimate_tokens(text: str) -> int:
    """粗略估计 token 数（中文按字符，英文按空格分词）"""
    import re
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    others = len(text) - chinese_chars - english_words
    return chinese_chars + english_words + others // 4


def _settings_value(key: str, fallback: str) -> str:
    """从全局 settings 读取配置值，任何异常回退到默认值（避免循环导入/启动顺序问题）"""
    try:
        from mclaw.config import settings as _settings

        return getattr(_settings, key, None) or fallback
    except Exception:
        return fallback


def _settings_opt_bool(key: str, fallback: bool | None = None) -> bool | None:
    """读取三态布尔配置：未设置返回 fallback（默认 None），显式值原样返回。

    ``_settings_value`` 用 ``or`` 兜底，会把显式的 False 当成"未设置"吞掉，
    所以布尔开关单独走这里。
    """
    try:
        from mclaw.config import settings as _settings

        val = getattr(_settings, key, None)
        return fallback if val is None else bool(val)
    except Exception:
        return fallback


# ── KnowledgeManager ───────────────────────────────────────────────────────────

class KnowledgeManager:
    """
    知识库管理器

    管理文档摄入、存储和混合检索。内部使用 SQLite 存储元数据，
    ChromaDB 存储向量（可降级到纯 FTS5）。
    """

    # reranker 精排的候选上限（见 search()：CPU 上是延迟与收益的折中点）
    RERANK_CANDIDATES = 15

    def __init__(
        self,
        data_dir: Path,
        model_name: str | None = None,
        device: str | None = None,
        download_source: str | None = None,
        reranker_enabled: bool | None = None,
        reranker_model: str | None = None,
        reranker_api_base: str | None = None,
        reranker_api_key: str | None = None,
        reranker_api_model: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name or _settings_value(
            "embedding_model", "BAAI/bge-small-zh-v1.5"
        )
        self.device = device or _settings_value("embedding_device", "cpu")
        self.download_source = download_source or _settings_value(
            "model_download_source", "auto"
        )

        # Reranker 精排配置（仅向量模式就绪后启用）
        self._reranker_model_name = reranker_model or _settings_value(
            "kb_reranker_model", "BAAI/bge-reranker-base"
        )
        self._reranker_api_base = reranker_api_base or _settings_value(
            "kb_reranker_api_base", ""
        )
        self._reranker_api_key = reranker_api_key or _settings_value(
            "kb_reranker_api_key", ""
        )
        self._reranker_api_model = reranker_api_model or _settings_value(
            "kb_reranker_api_model", "BAAI/bge-reranker-v2-m3"
        )

        # 开关三态：显式 True/False 照办；未设置时只在配了在线 Rerank API 时启用。
        # 本地模型约 1.1GB 且常驻内存，不在默认路径上主动触发下载。
        enabled = reranker_enabled
        if enabled is None:
            enabled = _settings_opt_bool("kb_reranker_enabled")
        if enabled is None:
            enabled = bool(self._reranker_api_base and self._reranker_api_key)
        self.reranker_enabled = bool(enabled)

        # ChromaDB state
        self._chroma_client = None
        self._chroma_collection = None
        self._embedding_model = None
        self._vector_enabled = False
        # 查询扩展（同义词/缩略语，FTS5 下的语义代偿）
        self._expand_query_enabled = bool(
            _settings_opt_bool("kb_query_expansion_enabled", True)
        )
        self._synonym_groups: list[list[str]] | None = None  # 懒加载

        self._reranker = None
        self._reindex_started = False
        self._reindex_progress: dict = {}
        self._reindex_last_stats: dict = {}
        self._init_lock = threading.RLock()
        self._init_started = False

        # SQLite database
        self._db_path = self.data_dir / "knowledge.db"
        self._init_sqlite()

        # Start background ChromaDB init
        self._start_chroma_init()

        # 精排独立于向量模式启动（在线 API 零依赖，纯 FTS5 下也能用）
        self._start_reranker_init()

    # ── SQLite ──────────────────────────────────────────────────────────────

    @staticmethod
    def _tokenize_for_fts(text: str) -> str:
        """Pre-tokenize text for FTS5 indexing.

        Splits CJK characters into space-separated bigrams so the unicode61
        tokenizer can index and match them. Non-CJK text passes through.
        """
        try:
            import jieba
            jieba.setLogLevel(logging.WARNING)
            return " ".join(jieba.cut_for_search(text))
        except ImportError:
            pass
        # Fallback: insert spaces between CJK characters
        import re
        result = re.sub(r"([一-鿿㐀-䶿])", r" \1 ", text)
        return re.sub(r"\s+", " ", result).strip()

    def _init_sqlite(self) -> None:
        """创建 SQLite 表结构"""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kb_collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                workspace_id TEXT DEFAULT 'default',
                owner_id TEXT DEFAULT '',
                is_public INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_documents (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT,
                file_type TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                checksum TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                chunk_count INTEGER DEFAULT 0,
                error_message TEXT,
                uploaded_by TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                FOREIGN KEY (collection_id) REFERENCES kb_collections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS kb_chunks (
                id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                content TEXT NOT NULL,
                chunk_index INTEGER DEFAULT 0,
                token_count INTEGER DEFAULT 0,
                embedding_id TEXT,
                metadata_json TEXT DEFAULT '{}',
                FOREIGN KEY (doc_id) REFERENCES kb_documents(id) ON DELETE CASCADE,
                FOREIGN KEY (collection_id) REFERENCES kb_collections(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                content,
                tokenize='unicode61'
            );

            CREATE INDEX IF NOT EXISTS idx_kb_docs_collection
                ON kb_documents(collection_id);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc
                ON kb_chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_collection
                ON kb_chunks(collection_id);
        """)
        # Migration: add uploaded_by to existing tables
        try:
            conn.execute("ALTER TABLE kb_documents ADD COLUMN uploaded_by TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add workspace/owner/public to collections (P1 multi-tenancy)
        for col, default in [("workspace_id", "'default'"), ("owner_id", "''"), ("is_public", "0")]:
            try:
                conn.execute(f"ALTER TABLE kb_collections ADD COLUMN {col} TEXT DEFAULT {default}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()
        logger.info(f"[KnowledgeManager] SQLite 初始化完成: {self._db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── ChromaDB 初始化 ─────────────────────────────────────────────────────

    def _start_chroma_init(self) -> None:
        """后台线程初始化 ChromaDB（不阻塞）"""
        with self._init_lock:
            if self._init_started:
                return
            self._init_started = True

        t = threading.Thread(
            target=self._do_chroma_init,
            name="KnowledgeManager-chroma-init",
            daemon=True,
        )
        t.start()

    # ── Reranker 初始化（与向量模式解耦）─────────────────────────────────────

    def _start_reranker_init(self) -> None:
        """后台线程装配精排器。

        精排是纯增强层，**不依赖 ChromaDB**：在线 Rerank API 只需要 httpx，
        纯 FTS5 模式下同样能对 BM25 候选重新排序。此前这段逻辑写在
        ``_do_chroma_init`` 的成功路径里，而该函数在没装 chromadb 时会提前
        return，导致没装 vector-memory 模块（约 2.5GB）的机器永远拿不到精排
        —— 哪怕配置的是零依赖的在线 API。
        """
        if not self.reranker_enabled:
            return

        def _run() -> None:
            try:
                from .reranker import Reranker

                reranker = Reranker(
                    model_name=self._reranker_model_name,
                    device=self.device,
                    download_source=self.download_source,
                    api_base=self._reranker_api_base,
                    api_key=self._reranker_api_key,
                    api_model=self._reranker_api_model,
                )
                with self._init_lock:
                    self._reranker = reranker
                reranker.start_background_init()
            except Exception as e:
                logger.warning(
                    f"[KnowledgeManager] reranker 装配失败，检索沿用 RRF 排序（非致命）: {e}"
                )

        threading.Thread(
            target=_run,
            name="KnowledgeManager-reranker-init",
            daemon=True,
        ).start()

    def _do_chroma_init(self) -> None:
        """实际 ChromaDB 初始化逻辑"""
        if not _lazy_import_chroma():
            logger.info("[KnowledgeManager] ChromaDB 不可用，使用纯 FTS5 模式")
            return

        try:
            # 加载 embedding 模型
            from .model_hub import load_embedding_model, _apply_source_env, _resolve_source, detect_best_source

            resolved = _resolve_source(self.download_source)
            if resolved.value == "auto":
                resolved = detect_best_source()
            _apply_source_env(resolved)

            logger.info(f"[KnowledgeManager] 加载 embedding 模型: {self.model_name}")
            model = load_embedding_model(
                model_name=self.model_name,
                source=self.download_source,
                device=self.device,
            )

            # 初始化 ChromaDB client
            chromadb_dir = self.data_dir / "chromadb"
            chromadb_dir.mkdir(parents=True, exist_ok=True)

            from chromadb.config import Settings

            client = _chromadb.PersistentClient(
                path=str(chromadb_dir),
                settings=Settings(anonymized_telemetry=False),
            )

            # 创建独立的 knowledge collection（metadata 记录 embedding 模型指纹）
            collection = client.get_or_create_collection(
                name="knowledge",
                metadata={"hnsw:space": "cosine"},
            )

            # ── embedding 模型切换检测 ──
            # 向量维度由模型决定（如 text2vec=768 维, bge-small=512 维），
            # 新旧模型的向量不能混在一个 collection 里。检测到模型变化时
            # 重建向量 collection 并后台重嵌入全部文档（FTS5 数据不受影响）。
            metadata = collection.metadata or {}
            existing_model = metadata.get("embedding_model", "")
            existing_count = collection.count()

            # 判定逻辑与 memories 向量库共用（见 needs_vector_rebuild 的注释）
            needs_reindex = needs_vector_rebuild(metadata, existing_count, self.model_name)

            if needs_reindex:
                if existing_model == self.model_name:
                    reason = "上次重建未完成（reindex_pending）"
                else:
                    reason = (
                        f"{existing_model or '(旧版无模型指纹)'} → {self.model_name}"
                        f"，存量向量 {existing_count} 条维度不兼容"
                    )
                logger.warning(f"[KnowledgeManager] 重建向量索引: {reason}")
                try:
                    client.delete_collection("knowledge")
                except Exception:
                    pass
                collection = client.get_or_create_collection(
                    name="knowledge",
                    metadata={
                        "hnsw:space": "cosine",
                        "embedding_model": self.model_name,
                        # 重建完成后由 reindex_all 清除；中途退出则下次启动重试
                        "reindex_pending": True,
                    },
                )
            elif not existing_model:
                try:
                    collection.modify(
                        metadata={
                            **metadata,
                            "embedding_model": self.model_name,
                        }
                    )
                except Exception as e:
                    logger.debug(f"[KnowledgeManager] 写入 embedding 指纹失败: {e}")

            with self._init_lock:
                self._embedding_model = model
                self._chroma_client = client
                self._chroma_collection = collection
                self._vector_enabled = True

            logger.info(
                f"[KnowledgeManager] ChromaDB ✓ 已就绪 "
                f"(collection='knowledge', {collection.count()} vectors, "
                f"model={self.model_name})"
            )

            # 精排的装配不在这里 —— 见 _start_reranker_init()，它与向量模式解耦，
            # 纯 FTS5 模式下同样可用（尤其在线 Rerank API）。

            # ── 模型切换后的全量重建（后台线程）──
            if needs_reindex:
                self._schedule_reindex_all(
                    reason=f"{existing_model or '(旧版无模型指纹)'} → {self.model_name}"
                )

        except Exception as e:
            logger.warning(
                f"[KnowledgeManager] ChromaDB 初始化失败，使用纯 FTS5 模式: {e}",
                exc_info=True,
            )

    # ── Collection CRUD ─────────────────────────────────────────────────────

    def create_collection(self, name: str, description: str = "",
                          workspace_id: str = "default", owner_id: str = "",
                          is_public: bool = False) -> KnowledgeCollection:
        now = time.time()
        coll = KnowledgeCollection(
            name=name,
            description=description,
            workspace_id=workspace_id,
            owner_id=owner_id,
            is_public=is_public,
            created_at=now,
            updated_at=now,
        )
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO kb_collections (id, name, description, workspace_id, "
            "owner_id, is_public, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (coll.id, coll.name, coll.description, coll.workspace_id,
             coll.owner_id, int(coll.is_public), coll.created_at, coll.updated_at),
        )
        conn.commit()
        conn.close()
        logger.info(f"[KnowledgeManager] 创建 collection: {coll.name} ({coll.id}) ws={workspace_id}")
        return coll

    def ensure_collection(self, collection_id: str, name: str, description: str = "",
                          workspace_id: str = "default", owner_id: str = "",
                          is_public: bool = False) -> bool:
        """幂等地创建一个指定稳定 id 的空知识集合（供 SYSTEM 预置骨架使用）。

        与 create_collection 的区别：id 由调用方提供（如 ``sys-dept-finance``），
        而非自动生成。id 是 kb_collections 的 TEXT 主键，调用方传入的 ``sys-``
        前缀首字符不是十六进制字符，永不与 uuid4 hex[:12] 自动 id 冲突。

        Returns:
            True 表示新建；False 表示该 id 已存在（或并发下他方已插入）。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            exists = conn.execute(
                "SELECT id FROM kb_collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if exists is not None:
                return False
            conn.execute(
                "INSERT INTO kb_collections (id, name, description, workspace_id, "
                "owner_id, is_public, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (collection_id, name, description, workspace_id,
                 owner_id, int(is_public), now, now),
            )
            conn.commit()
            logger.info(
                f"[KnowledgeManager] 确保预置 collection: {name} ({collection_id}) ws={workspace_id}"
            )
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        finally:
            conn.close()

    def list_collections(self, workspace_id: str | None = None,
                          offset: int = 0, limit: int = 50) -> tuple[list[KnowledgeCollection], int]:
        """列出 collections（分页），返回 (items, total)"""
        conn = self._get_conn()
        count_sql = "SELECT COUNT(*) FROM kb_collections"
        base_sql = (
            "SELECT c.*, "
            "  (SELECT COUNT(*) FROM kb_documents WHERE collection_id = c.id) AS doc_count, "
            "  (SELECT COUNT(*) FROM kb_chunks WHERE collection_id = c.id) AS chunk_count "
            "FROM kb_collections c"
        )
        params: tuple = ()
        if workspace_id:
            count_sql += " WHERE workspace_id = ?"
            base_sql += " WHERE c.workspace_id = ?"
            params = (workspace_id,)
        base_sql += " ORDER BY c.updated_at DESC LIMIT ? OFFSET ?"

        total = conn.execute(count_sql, params).fetchone()[0]  # type: ignore[arg-type]
        rows = conn.execute(base_sql, params + (limit, offset)).fetchall()  # type: ignore[operator]
        conn.close()
        # sqlite3.Row 不支持 .get()，用 dict(r) 转换
        return [
            KnowledgeCollection(
                id=r["id"], name=r["name"], description=r["description"],
                workspace_id=dict(r).get("workspace_id", "default"),
                owner_id=dict(r).get("owner_id", ""),
                is_public=bool(dict(r).get("is_public", 0)),
                created_at=r["created_at"], updated_at=r["updated_at"],
                doc_count=r["doc_count"], chunk_count=r["chunk_count"],
            )
            for r in rows
        ], total

    def get_collection(self, collection_id: str) -> KnowledgeCollection | None:
        conn = self._get_conn()
        r = conn.execute(
            "SELECT c.*, "
            "  (SELECT COUNT(*) FROM kb_documents WHERE collection_id = c.id) AS doc_count, "
            "  (SELECT COUNT(*) FROM kb_chunks WHERE collection_id = c.id) AS chunk_count "
            "FROM kb_collections c WHERE c.id = ?",
            (collection_id,),
        ).fetchone()
        conn.close()
        if r is None:
            return None
        d = dict(r)
        return KnowledgeCollection(
            id=d["id"], name=d["name"], description=d["description"],
            workspace_id=d.get("workspace_id", "default"),
            owner_id=d.get("owner_id", ""),
            is_public=bool(d.get("is_public", 0)),
            created_at=d["created_at"], updated_at=d["updated_at"],
            doc_count=d["doc_count"], chunk_count=d["chunk_count"],
        )

    def update_collection(self, collection_id: str, name: str | None = None, description: str | None = None) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(collection_id)

        conn = self._get_conn()
        conn.execute(f"UPDATE kb_collections SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        conn.close()
        return True

    def delete_collection(self, collection_id: str) -> int:
        """删除 collection 及其所有文档、chunks、向量，返回删除的 chunk 数"""
        conn = self._get_conn()
        # 统计 chunk 数（用于向量删除）
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM kb_chunks WHERE collection_id = ?", (collection_id,)
        ).fetchone()[0]

        # 收集 embedding_ids 用于删除向量
        emb_rows = conn.execute(
            "SELECT embedding_id FROM kb_chunks WHERE collection_id = ? AND embedding_id IS NOT NULL",
            (collection_id,),
        ).fetchall()
        emb_ids = [r["embedding_id"] for r in emb_rows if r["embedding_id"]]

        # Cascade delete: documents + chunks (FTS5 trigger handles kb_chunks_fts)
        conn.execute("DELETE FROM kb_chunks WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM kb_documents WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM kb_chunks_fts WHERE rowid NOT IN (SELECT rowid FROM kb_chunks)")
        conn.execute("DELETE FROM kb_collections WHERE id = ?", (collection_id,))
        conn.commit()
        conn.close()

        # 删除 ChromaDB 向量
        if emb_ids and self._chroma_collection:
            try:
                self._chroma_collection.delete(ids=emb_ids)
            except Exception as e:
                logger.warning(f"[KnowledgeManager] 删除向量失败: {e}")

        logger.info(f"[KnowledgeManager] 已删除 collection={collection_id} ({chunk_count} chunks)")
        return chunk_count

    def get_stats(self, collection_id: str) -> CollectionStats:
        conn = self._get_conn()
        # Use subqueries to avoid cross-join between documents and chunks
        r = conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM kb_documents WHERE collection_id = ?) AS doc_count, "
            "  (SELECT COUNT(*) FROM kb_chunks WHERE collection_id = ?) AS chunk_count, "
            "  (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM kb_chunks WHERE collection_id = ?) AS total_chars, "
            "  (SELECT COALESCE(SUM(file_size), 0) FROM kb_documents WHERE collection_id = ?) AS storage_bytes, "
            "  (SELECT COUNT(*) FROM kb_chunks WHERE collection_id = ? AND embedding_id IS NOT NULL) AS indexed_count, "
            "  (SELECT COUNT(*) FROM kb_documents WHERE collection_id = ? AND status = 'error') AS error_count",
            (collection_id,) * 6,
        ).fetchone()
        conn.close()
        return CollectionStats(
            collection_id=collection_id,
            doc_count=r["doc_count"] or 0,
            chunk_count=r["chunk_count"] or 0,
            total_chars=r["total_chars"] or 0,
            storage_bytes=r["storage_bytes"] or 0,
            indexed_count=r["indexed_count"] or 0,
            error_count=r["error_count"] or 0,
        )

    # ── Document CRUD ───────────────────────────────────────────────────────

    def list_documents(self, collection_id: str, offset: int = 0,
                        limit: int = 50) -> tuple[list[KnowledgeDocument], int]:
        """列出文档（分页），返回 (items, total)"""
        conn = self._get_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM kb_documents WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM kb_documents WHERE collection_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (collection_id, limit, offset),
        ).fetchall()
        conn.close()
        return [_doc_from_row(r) for r in rows], total

    def get_document(self, doc_id: str) -> KnowledgeDocument | None:
        conn = self._get_conn()
        r = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
        conn.close()
        return _doc_from_row(r) if r else None

    def get_document_chunks(self, doc_id: str) -> list[KnowledgeChunk]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index",
            (doc_id,),
        ).fetchall()
        conn.close()
        return [_chunk_from_row(r) for r in rows]

    def delete_document(self, doc_id: str) -> int:
        """删除文档及其 chunks 和向量"""
        conn = self._get_conn()
        doc = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            conn.close()
            return 0

        # 收集 embedding_ids
        emb_rows = conn.execute(
            "SELECT embedding_id FROM kb_chunks WHERE doc_id = ? AND embedding_id IS NOT NULL",
            (doc_id,),
        ).fetchall()
        emb_ids = [r["embedding_id"] for r in emb_rows]

        # 删除
        chunk_count = conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,)).rowcount
        conn.execute("DELETE FROM kb_chunks_fts WHERE rowid NOT IN (SELECT rowid FROM kb_chunks)")
        conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()

        # 删除向量
        if emb_ids and self._chroma_collection:
            try:
                self._chroma_collection.delete(ids=emb_ids)
            except Exception as e:
                logger.warning(f"[KnowledgeManager] 删除向量失败: {e}")

        logger.info(f"[KnowledgeManager] 已删除文档 {doc_id} ({chunk_count} chunks)")
        return chunk_count

    # ── 文档摄入 ────────────────────────────────────────────────────────────

    def ingest_file(self, collection_id: str, file_path: Path,
                    uploaded_by: str = "") -> KnowledgeDocument:
        """摄入单个文件：解析 → 分块 → 索引"""
        suffix = file_path.suffix.lower()
        if suffix not in EXTENSION_MAP:
            raise ValueError(f"不支持的文件格式: {suffix}")

        file_type = EXTENSION_MAP[suffix]
        file_size = file_path.stat().st_size
        checksum = _sha256_file(file_path)

        # 检查是否已存在（相同路径+内容）
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id, checksum FROM kb_documents WHERE collection_id = ? AND file_path = ?",
            (collection_id, str(file_path)),
        ).fetchone()
        if existing and existing["checksum"] == checksum:
            conn.close()
            logger.info(f"[KnowledgeManager] 文档未变更，跳过: {file_path.name}")
            return _doc_from_row(existing)

        # 创建文档记录
        now = time.time()
        doc = KnowledgeDocument(
            collection_id=collection_id,
            filename=file_path.name,
            file_path=str(file_path),
            file_type=file_type.value,
            file_size=file_size,
            checksum=checksum,
            status=DocStatus.PENDING.value,
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
        )

        if existing:
            # 更新已有记录
            doc.id = existing["id"]
            conn.execute(
                "UPDATE kb_documents SET checksum=?, status='pending', file_size=?, "
                "updated_at=? WHERE id=?",
                (checksum, file_size, now, doc.id),
            )
            # 删除旧 chunks
            conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc.id,))
        else:
            conn.execute(
                "INSERT INTO kb_documents (id, collection_id, filename, file_path, "
                "file_type, file_size, checksum, status, chunk_count, uploaded_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?,?)",
                (doc.id, doc.collection_id, doc.filename, doc.file_path,
                 doc.file_type, doc.file_size, doc.checksum, doc.status,
                 doc.uploaded_by, doc.created_at, doc.updated_at),
            )

        conn.commit()
        conn.close()

        # 执行索引
        try:
            self._index_document(doc, file_path, file_type)
        except Exception as e:
            self._mark_doc_error(doc.id, str(e))
            raise

        return self.get_document(doc.id) or doc

    def ingest_url(self, collection_id: str, url: str,
                   uploaded_by: str = "") -> KnowledgeDocument:
        """从 URL 摄入内容"""
        import hashlib as _hashlib

        now = time.time()
        url_hash = _hashlib.sha256(url.encode()).hexdigest()[:16]
        text = _extract_text_from_url(url)
        checksum = _hashlib.sha256(text.encode()).hexdigest()

        doc = KnowledgeDocument(
            collection_id=collection_id,
            filename=f"URL: {url[:80]}",
            file_type=DocFileType.URL.value,
            file_size=len(text.encode()),
            checksum=checksum,
            status=DocStatus.INDEXING.value,
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
            metadata={"source_url": url, "url_hash": url_hash},
        )

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO kb_documents (id, collection_id, filename, file_path, "
            "file_type, file_size, checksum, status, chunk_count, created_at, updated_at, metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,0,?,?,?)",
            (doc.id, doc.collection_id, doc.filename, None,
             doc.file_type, doc.file_size, doc.checksum, doc.status,
             doc.created_at, doc.updated_at, str(doc.metadata).replace("'", '"')),
        )
        conn.commit()
        conn.close()

        # Index chunks from extracted text
        chunks = _chunk_text(text)
        self._persist_chunks(doc, chunks)
        self._embed_chunks(doc, chunks)

        self._mark_doc_ready(doc.id, len(chunks))
        return self.get_document(doc.id) or doc

    def _index_document(self, doc: KnowledgeDocument, file_path: Path, file_type: DocFileType) -> None:
        """执行实际的文档索引"""
        self._mark_doc_status(doc.id, DocStatus.INDEXING)

        text = _extract_text(file_path, file_type)
        chunks = _chunk_text(text)

        if not chunks:
            suffix = file_path.suffix.lower()
            hint = ""
            if suffix == ".pdf":
                hint = (
                    "（PDF 可能为图片扫描件，文字提取为空。"
                    "请安装 Tesseract OCR 和 pytesseract："
                    "https://github.com/UB-Mannheim/tesseract/wiki）"
                )
            raise ValueError(f"文档内容为空: {file_path.name}{hint}")

        self._persist_chunks(doc, chunks)
        self._embed_chunks(doc, chunks)
        self._mark_doc_ready(doc.id, len(chunks))

        logger.info(
            f"[KnowledgeManager] 索引完成: {doc.filename} → {len(chunks)} chunks"
        )

    def _persist_chunks(self, doc: KnowledgeDocument, chunks: list[str]) -> None:
        """将 chunks 写入 SQLite + FTS5"""
        conn = self._get_conn()
        for i, text in enumerate(chunks):
            chunk_id = _short_uuid()
            token_count = _estimate_tokens(text)
            conn.execute(
                "INSERT INTO kb_chunks (id, doc_id, collection_id, content, chunk_index, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk_id, doc.id, doc.collection_id, text, i, token_count),
            )
            # 同步写入 FTS5（预分词）
            tokenized = self._tokenize_for_fts(text)
            conn.execute(
                "INSERT INTO kb_chunks_fts (rowid, content) VALUES ("
                "  (SELECT rowid FROM kb_chunks WHERE id = ?), ?"
                ")",
                (chunk_id, tokenized),
            )
        conn.commit()
        conn.close()

    def _embed_chunks(self, doc: KnowledgeDocument, chunks: list[str]) -> None:
        """向量化 chunks 并存入 ChromaDB"""
        if not self._vector_enabled or not self._embedding_model:
            logger.info("[KnowledgeManager] 向量未启用，跳过 embedding")
            return

        # 读取刚写入的 chunk ids
        conn = self._get_conn()
        chunk_rows = conn.execute(
            "SELECT id, content FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index",
            (doc.id,),
        ).fetchall()

        texts = [r["content"] for r in chunk_rows]
        chunk_ids = [r["id"] for r in chunk_rows]
        embedding_ids = [f"kb_{cid}" for cid in chunk_ids]

        try:
            # 生成 embeddings
            embeddings = self._embedding_model.encode(
                texts,
                show_progress_bar=False,
                batch_size=32,
            ).tolist()

            # 存入 ChromaDB
            self._chroma_collection.add(
                ids=embedding_ids,
                embeddings=embeddings,
                metadatas=[
                    {"chunk_id": cid, "doc_id": doc.id, "collection_id": doc.collection_id}
                    for cid in chunk_ids
                ],
            )

            # 更新 embedding_id
            for cid, eid in zip(chunk_ids, embedding_ids):
                conn.execute(
                    "UPDATE kb_chunks SET embedding_id = ? WHERE id = ?",
                    (eid, cid),
                )
            conn.commit()

            logger.info(
                f"[KnowledgeManager] 向量化完成: {doc.filename} → {len(chunks)} vectors"
            )

        except Exception as e:
            logger.warning(f"[KnowledgeManager] 向量化失败: {e}")
        finally:
            conn.close()

    def reindex_document(self, doc_id: str) -> KnowledgeDocument:
        """重新索引文档"""
        doc = self.get_document(doc_id)
        if not doc:
            raise ValueError(f"文档不存在: {doc_id}")

        if doc.file_type == DocFileType.URL.value:
            url = doc.metadata.get("source_url", "")
            if not url:
                raise ValueError("URL 文档缺少 source_url")
            text = _extract_text_from_url(url)
            if not text.strip():
                raise ValueError(
                    f"无法抓取 URL 内容（{url}），该链接可能已失效、需要登录或返回空页面"
                )
        elif doc.file_path:
            file_path = Path(doc.file_path)
            if not file_path.exists():
                raise FileNotFoundError(
                    f"文档文件已丢失: {file_path.name}，请删除此记录后重新上传"
                )
            file_type = DocFileType(doc.file_type) if doc.file_type in [e.value for e in DocFileType] else DocFileType.TEXT
            text = _extract_text(file_path, file_type)
        else:
            raise ValueError(f"无法定位文档内容: {doc_id}")

        # 删除旧 chunks 和向量
        old_emb = self._get_chunk_embedding_ids(doc_id)
        conn = self._get_conn()
        conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_chunks_fts WHERE rowid NOT IN (SELECT rowid FROM kb_chunks)")
        conn.commit()
        conn.close()
        if old_emb and self._chroma_collection:
            try:
                self._chroma_collection.delete(ids=old_emb)
            except Exception:
                pass

        # 重新分块索引
        chunks = _chunk_text(text)
        self._mark_doc_status(doc_id, DocStatus.INDEXING)
        self._persist_chunks(doc, chunks)
        self._embed_chunks(doc, chunks)
        self._mark_doc_ready(doc_id, len(chunks))

        return self.get_document(doc_id) or doc

    def reindex_all(self) -> dict[str, int]:
        """
        重建全部文档索引（分块 + FTS5 + 向量重嵌入）。

        用途：embedding 模型切换后新旧向量维度不兼容，需要全量重嵌入。
        单文档失败不影响其余文档，失败计数记入返回值。

        Returns:
            {"total": 总数, "success": 成功数, "failed": 失败数}
        """
        conn = self._get_conn()
        doc_ids = [
            r["id"] for r in conn.execute("SELECT id FROM kb_documents").fetchall()
        ]
        conn.close()

        total = len(doc_ids)
        with self._init_lock:
            self._reindex_progress = {"total": total, "done": 0}

        success = failed = 0
        for i, doc_id in enumerate(doc_ids, 1):
            try:
                self.reindex_document(doc_id)
                success += 1
            except Exception as e:
                failed += 1
                try:
                    self._mark_doc_error(doc_id, str(e))
                except Exception:
                    pass
                logger.warning(
                    f"[KnowledgeManager] 重建失败 ({i}/{len(doc_ids)}): doc={doc_id}: {e}"
                )
            with self._init_lock:
                self._reindex_progress = {"total": total, "done": i}
            if i % 20 == 0 or i == total:
                logger.info(
                    f"[KnowledgeManager] 重建进度: {i}/{total} "
                    f"(成功 {success}, 失败 {failed})"
                )

        stats = {"total": total, "success": success, "failed": failed}
        # 全部成功才清除 pending 标记：否则留到下次启动继续重试，
        # 避免"重建到一半失败"的库被永久标记成已完成。
        if total and not failed:
            self._clear_reindex_pending()
        return stats

    def schedule_reindex_all(self, reason: str = "") -> bool:
        """
        触发后台全量重建索引。

        Returns:
            True 表示本次已启动；False 表示已有重建任务在跑（不重复启动）。
        """
        with self._init_lock:
            if self._reindex_started:
                return False
            self._reindex_started = True

        def _run() -> None:
            logger.info(f"[KnowledgeManager] 开始全量重建索引 ({reason})")
            try:
                stats = self.reindex_all()
                with self._init_lock:
                    self._reindex_last_stats = {**stats, "reason": reason, "ok": True}
                logger.info(
                    f"[KnowledgeManager] 全量重建完成: {stats['success']}/{stats['total']} "
                    f"成功, {stats['failed']} 失败"
                )
            except Exception as e:
                with self._init_lock:
                    self._reindex_last_stats = {"reason": reason, "ok": False, "error": str(e)}
                logger.error(f"[KnowledgeManager] 全量重建异常终止: {e}", exc_info=True)
            finally:
                with self._init_lock:
                    self._reindex_started = False

        t = threading.Thread(
            target=_run,
            name="KnowledgeManager-reindex-all",
            daemon=True,
        )
        t.start()
        return True

    # 兼容旧调用名（内部启动路径使用）
    _schedule_reindex_all = schedule_reindex_all

    def reindex_status(self) -> dict:
        """全量重建进度：running / progress / last 结果"""
        with self._init_lock:
            return {
                "running": self._reindex_started,
                "progress": dict(self._reindex_progress or {}),
                "last": dict(self._reindex_last_stats or {}) or None,
                "vector_enabled": self._vector_enabled,
                "model": self.model_name,
            }

    def _clear_reindex_pending(self) -> None:
        """重建成功后清除 collection 上的 pending 标记"""
        with self._init_lock:
            coll = self._chroma_collection
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
            logger.debug(f"[KnowledgeManager] 清除 reindex_pending 失败: {e}")

    # ── 混合检索 ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection_id: str | None = None,
        top_k: int = 10,
    ) -> list[SearchResult]:
        """
        混合检索：BM25(FTS5+Jieba) + 向量(ChromaDB) → RRF 融合 → Reranker 精排

        Reranker 为可选增强层，与向量模式无关（在线 API 零依赖）：未就绪 /
        加载中 / 失败时自动沿用 RRF 排序；纯 FTS5 模式下它直接对 BM25 候选
        重新排序。

        Args:
            query: 搜索查询
            collection_id: 可选，限定 collection
            top_k: 返回结果数
        """
        bm25_results = self._search_bm25(query, collection_id, top_k * 2)
        vector_results = self._search_vector(query, collection_id, top_k * 2)
        merged = self._rrf_fusion(bm25_results, vector_results, k=60)
        merged.sort(key=lambda x: x.score, reverse=True)

        # Reranker 精排：对 RRF 融合后的候选逐对精细打分。
        # 候选数刻意压到 15：cross-encoder 是 O(n) 次前向，CPU 上每对百毫秒级，
        # 30 条会让单次检索多出数秒，而精排收益集中在头几条。
        reranker = self._reranker
        if reranker is not None and reranker.available and len(merged) > 1:
            candidate_count = min(self.RERANK_CANDIDATES, len(merged))
            candidates = merged[:candidate_count]
            reranked = self._rerank_results(query, candidates, reranker)
            if reranked is not None:
                return reranked[:top_k]

        return merged[:top_k]

    def _rerank_results(
        self,
        query: str,
        candidates: list[SearchResult],
        reranker,
    ) -> list[SearchResult] | None:
        """用 reranker 对候选结果精排；失败返回 None（沿用原排序）"""
        chunk_ids = [r.chunk.id for r in candidates]
        contents = self._fetch_chunk_contents(chunk_ids)
        if len(contents) != len(candidates):
            # 部分 chunk 已被删除等异常情况，放弃精排
            return None

        try:
            scores = reranker.rerank(query, [contents[cid] for cid in chunk_ids])
        except Exception as e:
            logger.warning(f"[KnowledgeManager] reranker 精排异常，沿用 RRF 排序: {e}")
            return None
        if scores is None:
            return None

        for r, score in zip(candidates, scores):
            r.score = score
            if not r.source_type.endswith("+rerank"):
                r.source_type = f"{r.source_type}+rerank"
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates

    def _fetch_chunk_contents(self, chunk_ids: list[str]) -> dict[str, str]:
        """批量读取 chunk 内容（精排需要原文）"""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"SELECT id, content FROM kb_chunks WHERE id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
            return {r["id"]: r["content"] for r in rows}
        except Exception:
            return {}
        finally:
            conn.close()

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Make user input safe for FTS5 MATCH."""
        special = set('"*(){}[]^~:')
        cleaned = "".join(c if c not in special else " " for c in query)
        tokens = cleaned.split()
        if not tokens:
            return '""'
        return " OR ".join(tokens)

    def _synonyms(self) -> list[list[str]]:
        """同义词等价组（内置表 + data_dir/synonyms.json），懒加载并缓存"""
        if self._synonym_groups is None:
            try:
                from .query_expansion import load_synonym_groups

                groups = load_synonym_groups(self.data_dir / "synonyms.json")
            except Exception as e:
                logger.warning(f"[KnowledgeManager] 同义词表加载失败，跳过查询扩展: {e}")
                groups = []
            with self._init_lock:
                self._synonym_groups = groups
        return self._synonym_groups

    def _expansion_terms(self, query: str) -> list[str]:
        """同义词扩展词（已按索引同一套分词切好）；未启用或没命中则为空"""
        if not self._expand_query_enabled:
            return []
        try:
            from .query_expansion import expand_query

            extras = expand_query(query, self._synonyms())
        except Exception as e:
            logger.debug(f"[KnowledgeManager] 查询扩展跳过: {e}")
            return []
        if not extras:
            return []
        try:
            import jieba

            pieces: list[str] = []
            for t in extras:
                pieces.extend(jieba.cut_for_search(t))
            return pieces
        except ImportError:
            return extras

    @staticmethod
    def _segment(text: str) -> str:
        """按索引同一套分词切分（索引里存的是 jieba 切过的 token）"""
        try:
            import jieba

            return " ".join(jieba.cut_for_search(text))
        except ImportError:
            return text

    def _fts_match(
        self, safe_query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        """执行一次 FTS5 MATCH，返回 chunk_id -> 分数（越大越相关）"""
        conn = self._get_conn()
        result: dict[str, float] = {}
        try:
            if collection_id:
                rows = conn.execute(
                    "SELECT c.id, c.content, bm25(kb_chunks_fts) AS rank "
                    "FROM kb_chunks_fts f "
                    "JOIN kb_chunks c ON c.rowid = f.rowid "
                    "WHERE kb_chunks_fts MATCH ? AND c.collection_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (safe_query, collection_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.id, bm25(kb_chunks_fts) AS rank "
                    "FROM kb_chunks_fts f "
                    "JOIN kb_chunks c ON c.rowid = f.rowid "
                    "WHERE kb_chunks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (safe_query, limit),
                ).fetchall()
        except Exception:
            logger.debug("FTS query failed", exc_info=True)
            rows = []
        finally:
            conn.close()

        # Convert BM25 rank to score: lower rank = better -> score = 1/(1+rank)
        for r in rows:
            rank = r["rank"] if isinstance(r, sqlite3.Row) else r[1]
            chunk_id = r["id"] if isinstance(r, sqlite3.Row) else r[0]
            result[chunk_id] = 1.0 / (1.0 + rank)
        return result

    @staticmethod
    def _merge_expansion(
        primary: dict[str, float],
        extra: dict[str, float],
        weight: float = 0.4,
    ) -> dict[str, float]:
        """原查询结果为准，同义词扩展结果**降权**叠加（只补召回，不抢排序）。

        为什么不把扩展词直接 OR 进原查询：实测那样做 precision 会崩——
        关键词明确的问题从 10/10 掉到 8/10，因为 BM25 按"命中词数"给命中多个
        扩展词的无关 chunk 排到了前面。这里改成两级：原查询命中的 chunk 永远
        排在"只靠同义词命中"的 chunk 之前。
        """
        k = 60
        merged: dict[str, float] = {}
        for i, (cid, _s) in enumerate(
            sorted(primary.items(), key=lambda x: x[1], reverse=True), 1
        ):
            merged[cid] = 1.0 / (k + i)
        for i, (cid, _s) in enumerate(
            sorted(extra.items(), key=lambda x: x[1], reverse=True), 1
        ):
            if cid in merged:
                continue  # 原查询已命中，不重复计分
            merged[cid] = weight / (k + i)
        return merged

    def _search_bm25(
        self, query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        """FTS5 + jieba BM25 搜索：原查询为主，同义词扩展补召回"""
        result = self._fts_match(
            self._sanitize_fts_query(self._segment(query)), collection_id, limit
        )

        extras = self._expansion_terms(query)
        if extras:
            expanded = self._fts_match(
                self._sanitize_fts_query(" ".join(extras)), collection_id, limit
            )
            if expanded:
                logger.debug(f"[KnowledgeManager] 查询扩展补召回: +{len(extras)} 词")
                result = self._merge_expansion(result, expanded)
        return result

    def _search_vector(
        self, query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        """ChromaDB 向量搜索"""
        if not self._vector_enabled or not self._chroma_collection:
            return {}

        try:
            embedding = self._embedding_model.encode(
                [query], show_progress_bar=False
            ).tolist()[0]

            where_filter = None
            if collection_id:
                where_filter = {"collection_id": collection_id}

            results = self._chroma_collection.query(
                query_embeddings=[embedding],
                n_results=limit,
                where=where_filter,
                include=["metadatas", "distances"],
            )

            scores: dict[str, float] = {}
            if results["ids"] and results["ids"][0]:
                ids_list = results["ids"][0]
                dists_list = results["distances"][0]
                mds_list = results.get("metadatas", [[]])[0] or []
                for idx, (cid, dist) in enumerate(zip(ids_list, dists_list)):
                    # metadatas[idx] is a dict: {"chunk_id": "...", "collection_id": "..."}
                    meta = mds_list[idx] if idx < len(mds_list) else {}
                    chunk_id = meta.get("chunk_id", "") if isinstance(meta, dict) else ""
                    key = chunk_id if chunk_id else cid
                    scores[key] = max(0, 1.0 - dist)
            return scores

        except Exception as e:
            logger.warning(f"[KnowledgeManager] 向量搜索失败: {e}")
            return {}

    @staticmethod
    def _rrf_fusion(
        bm25_scores: dict[str, float],
        vector_scores: dict[str, float],
        k: int = 60,
    ) -> list[SearchResult]:
        """Reciprocal Rank Fusion 融合 BM25 和向量排名"""
        def _ranked(scores: dict[str, float]) -> dict[str, int]:
            sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            return {chunk_id: rank + 1 for rank, (chunk_id, _) in enumerate(sorted_items)}

        bm25_ranks = _ranked(bm25_scores)
        vector_ranks = _ranked(vector_scores)

        all_chunk_ids = set(bm25_ranks.keys()) | set(vector_ranks.keys())
        results: list[SearchResult] = []
        from dataclasses import field as _field

        for chunk_id in all_chunk_ids:
            bm25_rank = bm25_ranks.get(chunk_id, len(bm25_ranks) + 1)
            vector_rank = vector_ranks.get(chunk_id, len(vector_ranks) + 1)
            rrf_score = 1.0 / (k + bm25_rank) + 1.0 / (k + vector_rank)

            source = "hybrid"
            if chunk_id in bm25_scores and chunk_id not in vector_scores:
                source = "bm25"
            elif chunk_id in vector_scores and chunk_id not in bm25_scores:
                source = "vector"

            # Create a minimal result (chunk details loaded by caller if needed)
            chunk = KnowledgeChunk(id=chunk_id)
            results.append(SearchResult(
                chunk=chunk,
                score=rrf_score,
                bm25_score=bm25_scores.get(chunk_id, 0),
                vector_score=vector_scores.get(chunk_id, 0),
                source_type=source,
            ))

        return results

    def search_with_chunks(
        self,
        query: str,
        collection_id: str | None = None,
        top_k: int = 10,
    ) -> list[SearchResult]:
        """搜索并填充完整的 chunk 内容（含来源文件名）"""
        results = self.search(query, collection_id, top_k)
        conn = self._get_conn()

        filled: list[SearchResult] = []
        for r in results:
            row = conn.execute(
                "SELECT c.*, d.filename AS doc_filename "
                "FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id "
                "WHERE c.id = ?",
                (r.chunk.id,),
            ).fetchone()
            if row:
                r.chunk = _chunk_from_row(row)
                doc_filename = dict(row).get("doc_filename", "")
                if doc_filename and r.chunk.metadata is not None:
                    r.chunk.metadata["filename"] = doc_filename
                filled.append(r)

        conn.close()
        return filled

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _get_chunk_embedding_ids(self, doc_id: str) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT embedding_id FROM kb_chunks WHERE doc_id = ? AND embedding_id IS NOT NULL",
            (doc_id,),
        ).fetchall()
        conn.close()
        return [r["embedding_id"] for r in rows]

    def _mark_doc_status(self, doc_id: str, status: DocStatus) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE kb_documents SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, time.time(), doc_id),
        )
        conn.commit()
        conn.close()

    def _mark_doc_ready(self, doc_id: str, chunk_count: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE kb_documents SET status = ?, chunk_count = ?, error_message = NULL, "
            "updated_at = ? WHERE id = ?",
            (DocStatus.READY.value, chunk_count, time.time(), doc_id),
        )
        conn.commit()
        conn.close()

    def _mark_doc_error(self, doc_id: str, error: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE kb_documents SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
            (DocStatus.ERROR.value, error, time.time(), doc_id),
        )
        conn.commit()
        conn.close()


# ── SQLite row → dataclass converters ─────────────────────────────────────────

def _doc_from_row(r: sqlite3.Row | dict[str, Any]) -> KnowledgeDocument:
    if isinstance(r, sqlite3.Row):
        d = dict(r)
    else:
        d = r
    import json as _json
    metadata = {}
    if d.get("metadata_json"):
        try:
            metadata = _json.loads(d["metadata_json"])
        except Exception:
            pass
    return KnowledgeDocument(
        id=d["id"], collection_id=d["collection_id"],
        filename=d["filename"], file_path=d.get("file_path"),
        file_type=d["file_type"], file_size=d.get("file_size", 0),
        checksum=d.get("checksum", ""), status=d.get("status", "pending"),
        chunk_count=d.get("chunk_count", 0),
        error_message=d.get("error_message"),
        uploaded_by=d.get("uploaded_by", ""),
        created_at=d.get("created_at", 0.0), updated_at=d.get("updated_at", 0.0),
        metadata=metadata,
    )


def _chunk_from_row(r: sqlite3.Row | dict[str, Any]) -> KnowledgeChunk:
    if isinstance(r, sqlite3.Row):
        d = dict(r)
    else:
        d = r
    import json as _json
    metadata = {}
    if d.get("metadata_json"):
        try:
            metadata = _json.loads(d["metadata_json"])
        except Exception:
            pass
    return KnowledgeChunk(
        id=d["id"], doc_id=d["doc_id"], collection_id=d["collection_id"],
        content=d.get("content", ""), chunk_index=d.get("chunk_index", 0),
        token_count=d.get("token_count", 0),
        embedding_id=d.get("embedding_id"),
        metadata=metadata,
    )


# ── Utils ─────────────────────────────────────────────────────────────────────

def _sha256_file(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
