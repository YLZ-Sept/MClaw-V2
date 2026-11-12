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
import json
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

# 扫描页判定：一页提取出的文字少于此长度，就当作图片页走 OCR。
# 空白页/签字页/盖章页本来就没多少字，也会被 OCR 覆盖；OCR 结果为空时
# 保留 pdfplumber 的原文，不会比现在更差。
_PDF_PAGE_MIN_TEXT_CHARS = 10
# 单份 PDF 最多 OCR 的页数，防止几百页的扫描件把上传请求拖死。
_DEFAULT_OCR_MAX_PAGES = 200

_rapidocr_engine: Any = None
_rapidocr_lock = threading.Lock()
# OCR 推理本身也串行化：上传与后台重建可能同时跑，多个 onnx 会话
# 并发只会互相抢 CPU，延迟反而更差。
_ocr_infer_lock = threading.Lock()


def _get_ocr_engine() -> Any:
    """惰性初始化 RapidOCR。

    PP-OCRv4 的 det/rec/cls 三个 onnx 模型在 wheel 内（约 16MB），
    首次初始化约 1–2 秒，之后复用同一个 engine。
    """
    global _rapidocr_engine
    if _rapidocr_engine is not None:
        return _rapidocr_engine
    with _rapidocr_lock:
        if _rapidocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _rapidocr_engine = RapidOCR()
    return _rapidocr_engine


def _ocr_max_pages() -> int:
    try:
        return max(1, int(os.environ.get("KB_OCR_MAX_PAGES", _DEFAULT_OCR_MAX_PAGES)))
    except ValueError:
        return _DEFAULT_OCR_MAX_PAGES


def _ocr_pdf_pages(
    file_path: Path, page_indexes: list[int], max_pages: int
) -> dict[int, str]:
    """把指定页渲染成 200 DPI 图片后 OCR，返回 {页序号: 文本}。

    只渲染需要 OCR 的页，混合型 PDF（前几页文字、后面扫描）也能处理。
    依赖缺失或单页失败时返回空结果，由调用方按原文继续，不阻断导入。
    """
    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("[OCR] 缺少 pypdfium2/numpy，无法对扫描件做 OCR")
        return {}

    try:
        engine = _get_ocr_engine()
    except ImportError:
        logger.warning("[OCR] rapidocr-onnxruntime 未安装，跳过扫描页 OCR")
        return {}
    except Exception as e:
        logger.warning(f"[OCR] RapidOCR 初始化失败: {e}")
        return {}

    selected = page_indexes[:max_pages]
    if len(page_indexes) > max_pages:
        # 不静默截断：用户需要知道这份文档只索引了前 N 个扫描页
        logger.warning(
            "[OCR] %s 有 %d 个扫描页，超过上限 %d，仅 OCR 前 %d 页；"
            "可调大 KB_OCR_MAX_PAGES 或拆分 PDF 后重新上传",
            file_path.name, len(page_indexes), max_pages, max_pages,
        )

    ocr_texts: dict[int, str] = {}
    pdf = pdfium.PdfDocument(str(file_path))
    try:
        for idx in selected:
            try:
                bitmap = pdf[idx].render(scale=200 / 72)
                image = bitmap.to_pil().convert("RGB")
                with _ocr_infer_lock:
                    result, _ = engine(np.array(image))
            except Exception as e:
                logger.warning(
                    f"[OCR] {file_path.name} 第 {idx + 1} 页识别失败: {e}"
                )
                continue

            lines: list[tuple[float, float, str]] = []
            for item in result or []:
                box, text = item[0], item[1]
                if not text or not str(text).strip():
                    continue
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                lines.append((
                    sum(ys) / len(ys),
                    sum(xs) / len(xs),
                    str(text).strip(),
                ))
            if not lines:
                continue
            # 检测框按“行”排序：同一行的 y 略有抖动，先按 8px 分桶再从左到右，
            # 避免表格单元格的顺序错乱。
            lines.sort(key=lambda line: (round(line[0] / 8), line[1]))
            ocr_texts[idx] = "\n".join(line[2] for line in lines)
    finally:
        pdf.close()

    if ocr_texts:
        logger.info(
            "[OCR] %s 完成 %d/%d 个扫描页识别",
            file_path.name, len(ocr_texts), len(selected),
        )
    return ocr_texts


def _extract_pdf_text(file_path: Path) -> str:
    """提取 PDF 文本；纯图片页回退到本地 OCR。

    逐页判定而不是“整份都没文字才 OCR”，否则混合型 PDF（前几页正文、
    后面扫描）会整份丢掉扫描部分。
    """
    page_texts: list[str] = []

    # 第 1 步: pdfplumber（文字型页面）
    try:
        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            page_texts = [page.extract_text() or "" for page in pdf.pages]
    except ImportError:
        # 回退到 PyPDF2
        try:
            from PyPDF2 import PdfReader

            reader = PdfReader(str(file_path))
            page_texts = [page.extract_text() or "" for page in reader.pages]
        except ImportError:
            raise ImportError(
                "PDF 解析需要安装 pdfplumber 或 PyPDF2: "
                "pip install pdfplumber"
            )

    # 第 2 步: 文字过少的页当作扫描页，交给本地 OCR
    scanned_pages = [
        i for i, text in enumerate(page_texts)
        if len(text.strip()) < _PDF_PAGE_MIN_TEXT_CHARS
    ]
    if scanned_pages:
        ocr_texts = _ocr_pdf_pages(file_path, scanned_pages, _ocr_max_pages())
        # 只有 OCR 出文字才覆盖原页；识别失败时保留 pdfplumber 的结果
        for idx, text in ocr_texts.items():
            page_texts[idx] = text

    return "\n\n".join(text.strip() for text in page_texts if text.strip())


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
        return _extract_pdf_text(file_path)

    if file_type == DocFileType.DOCX:
        try:
            from docx import Document
            from docx.oxml.ns import qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph

            doc = Document(str(file_path))
            # doc.paragraphs 不包含表格内容 —— 合同/调查表/评估表的主体往往
            # 就在表格里，只读 paragraphs 会静默丢掉大半文档。这里按 body
            # 子元素顺序遍历，段落与表格都提取；表格行用制表符连接，交给
            # 分块器识别为表格并保留表头。
            segments: list[str] = []
            for child in doc.element.body.iterchildren():
                if child.tag == qn("w:p"):
                    text = Paragraph(child, doc).text.strip()
                    if text:
                        segments.append(text)
                elif child.tag == qn("w:tbl"):
                    rows: list[str] = []
                    for row in Table(child, doc).rows:
                        cells: list[str] = []
                        for cell in row.cells:
                            text = cell.text.strip().replace("\n", " ")
                            # 横向合并的单元格会被 python-docx 按网格重复返回
                            # （同一个地址重复 3 次），连续相同值合并一次。
                            if not cells or cells[-1] != text:
                                cells.append(text)
                        line = "\t".join(cells)
                        if line.strip():
                            rows.append(line)
                    if rows:
                        segments.append("\n".join(rows))
            return "\n\n".join(segments)
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

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_BOLD_TITLE_RE = re.compile(r"^\s*\*\*(?!\s*\*)(.+?)\*\*\s*$")
_SHEET_TITLE_RE = re.compile(r"^\s*===\s*Sheet:\s*(.+?)\s*===\s*$")
_SLIDE_TITLE_RE = re.compile(r"^\s*===\s*第\s*(\d+)\s*页\s*===\s*$")
_CN_HEADING_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百零0-9]+[章节部分卷篇]|扩展卷|[一二三四五六七八九十]+、)\s*\S"
)


def _section_title(line: str) -> str | None:
    """识别会开启新章节的行。

    支持 Markdown 标题、整行加粗（Word 转 Markdown 常见）、Excel 工作表
    标记、PPT 页码标记，以及 Word 原生文档里的「一、」「第三章」小标题。
    """
    m = _HEADING_RE.match(line)
    if m:
        return m.group(2).strip()
    m = _SHEET_TITLE_RE.match(line)
    if m:
        return f"工作表：{m.group(1).strip()}"
    m = _SLIDE_TITLE_RE.match(line)
    if m:
        return f"第 {m.group(1)} 页"
    m = _BOLD_TITLE_RE.match(line)
    if m:
        # 整行加粗在转换自 Word 的 Markdown 里很常见（**一、本周工作重点**）。
        # 句子中间的局部加粗不会独占一行，因此这里只接受整行。
        return m.group(1).strip().strip("*").strip()

    # Word 原生文档没有 Markdown 标记，靠「一、」「第三章」这类短行分节。
    # 限制长度和结尾标点，避免把正文里的编号句子误判成标题。
    s = line.strip()
    if (
        not _is_table_line(line)
        and len(s) <= 40
        and _CN_HEADING_RE.match(s)
        and not s.endswith(("。", "；", ";", "，", ",", ".", "！", "？"))
    ):
        return s
    return None


def _is_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if "\t" in s:
        return len([c for c in s.split("\t") if c.strip()]) >= 2
    if "|" in s:
        return s.count("|") >= 2 or " | " in s
    return False


def _row_cells(line: str) -> list[str]:
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _normalize_table_row(line: str) -> str:
    """压掉表格行里用于对齐的连续空格，同时保留每个单元格的内容"""
    if "\t" in line:
        return "\t".join(c.strip() for c in line.split("\t")).strip()
    if "|" in line:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return "| " + " | ".join(cells) + " |"
    return line.strip()


def _is_empty_table_row(line: str) -> bool:
    cells = _row_cells(line)
    return all(not c for c in cells)


def _is_table_separator(line: str) -> bool:
    cells = [c for c in _row_cells(line) if c]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按标题把文本切成 (章节名, 正文)。

    转换自 Word 的周报常用 ``**一、本周工作重点**`` 这种整行加粗当小标题，
    Excel 提取则用 ``=== Sheet: xxx ===`` 当分隔，两者都要识别，否则表格
    会跨章节混在一起。
    """
    sections: list[tuple[str, str]] = []
    title = ""
    buf: list[str] = []
    # 只有标题、没有正文的章节（如文档大标题后面紧跟小标题）不能丢，
    # 把标题文本顺延到下一个有正文的章节里，否则“项目周报”这种信息会消失。
    pending: list[str] = []
    empty_run = 0

    def flush() -> None:
        nonlocal buf, empty_run
        if any(line.strip() for line in buf):
            sections.append((title, "\n".join(buf)))
            empty_run = 0
        elif title:
            pending.append(title)
            empty_run += 1
        buf = []

    for line in text.splitlines():
        heading = _section_title(line)
        if heading is not None:
            flush()
            # 连续两个以上“只有标题”的章节基本就是目录页
            # （第一章/第二章/…每行都没正文）。这类标题链既没有内容，
            # 还会让前缀膨胀成“第一章 / 第二章 / …”，直接断开。
            if empty_run >= 2:
                pending.clear()
                title = heading
            else:
                title = " / ".join([*pending, heading]) if pending else heading
                pending.clear()
            continue
        buf.append(line)
    flush()
    if pending:
        joined_pending = "\n".join(pending)
        if sections:
            last_title, last_body = sections[-1]
            sections[-1] = (last_title, f"{last_body}\n{joined_pending}")
        else:
            sections.append((title, joined_pending))
    if not sections:
        return [("", text)] if text.strip() else []
    return sections


def _split_blocks(body: str) -> list[tuple[str, list[str]]]:
    """把章节正文切成连续的表格块 / 文本块"""
    blocks: list[tuple[str, list[str]]] = []
    cur: list[str] = []
    kind = ""

    def flush() -> None:
        nonlocal cur, kind
        if cur:
            blocks.append((kind or "text", cur))
        cur, kind = [], ""

    for line in body.splitlines():
        if not line.strip():
            flush()
            continue
        k = "table" if _is_table_line(line) else "text"
        if kind and k != kind:
            flush()
        kind = k
        cur.append(line)
    flush()
    return blocks


def _pack_sentences(text: str, limit: int) -> list[str]:
    """把超长段落按句子打包成不超过 limit 的块（单句超长时硬切）"""
    chunks: list[str] = []
    buf = ""
    for s in _split_sentences(text):
        # 没有标点、整段一句的情况（Excel 导出的长描述很常见）：
        # 先硬切成 limit 长度的片段，避免单块无限膨胀。
        while len(s) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(s[:limit])
            s = s[limit:]
        if len(buf) + len(s) <= limit:
            buf = f"{buf}{s}" if buf else s
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks


def _split_text_chunks(text: str, chunk_size: int) -> list[str]:
    """段落优先的正文分块（不含章节前缀、不做 overlap）"""
    chunks: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) > chunk_size:
            chunks.extend(_pack_sentences(para, chunk_size))
        else:
            current = para
    if current:
        chunks.append(current)
    return chunks


def _split_oversized_row(
    row: str, header: str, prefix_len: int, chunk_size: int
) -> list[str]:
    """单行表格超过预算时按单元格内容拆开，并保留标识列。

    周报里的“工作内容”一列常长达上千字；如果直接按字数硬切，后半段会丢掉
    客户名称/项目名称这些标识列，模型拿到也判断不出说的是哪个项目。
    这里只拆最后一个单元格，每段都重复前面的标识列。
    """
    cells = _row_cells(row)
    budget = max(chunk_size - prefix_len - len(header) - 2, 64)

    if len(cells) <= 1:
        return _pack_sentences(row, budget)

    lead = cells[:-1]
    lead_text = (
        "\t".join(lead) if "\t" in row else "| " + " | ".join(lead) + " |"
    )
    # 标识列本身也占长度，真正能放正文的空间要再扣掉
    content_budget = max(budget - len(lead_text) - 2, 64)

    pieces = [
        p.strip()
        for p in re.split(r"<br\s*/?>|(?<=[。；;！？!?])", cells[-1])
        if p.strip()
    ] or [cells[-1]]

    # 同一行的多个片段尽量打包回一个 chunk：每条 <br> 都单独成块会让
    # 一篇周报膨胀出上百个 chunk，稀释检索名额。
    out: list[str] = []
    current = ""
    for piece in pieces:
        parts = _pack_sentences(piece, content_budget) if len(piece) > content_budget else [piece]
        for part in parts:
            if current and len(current) + len(part) + 2 > content_budget:
                out.append(_format_row(lead, current, row, header))
                current = part
            else:
                current = f"{current}<br>{part}" if current else part
    if current:
        out.append(_format_row(lead, current, row, header))
    return out


def _format_row(lead: list[str], content: str, row: str, header: str) -> str:
    if "\t" in row:
        line = "\t".join([*lead, content])
    else:
        line = "| " + " | ".join([*lead, content]) + " |"
    return (header + "\n" if header else "") + line


def _chunk_table(lines: list[str], prefix_len: int, chunk_size: int) -> list[str]:
    """表格分块：保留表头并在每一块里重复，避免切出无列名的裸行"""
    rows = [_normalize_table_row(line) for line in lines if line.strip()]
    rows = [r for r in rows if r]
    if not rows:
        return []

    # 找到分隔行。转换自 Word 的表格可能是「空表头 / 分隔行 / 真表头」，
    # 标准 Markdown 则是「表头 / 分隔行 / 数据行」，两种都要兼容。
    sep_idx = next((i for i, r in enumerate(rows) if _is_table_separator(r)), None)
    if sep_idx is not None:
        before = rows[sep_idx - 1] if sep_idx > 0 else ""
        after = rows[sep_idx + 1] if sep_idx + 1 < len(rows) else ""
        if before and not _is_empty_table_row(before) and not _is_table_separator(before):
            header = before
            body_rows = rows[sep_idx + 1:]
        elif after and not _is_empty_table_row(after):
            header = after
            body_rows = rows[sep_idx + 2:]
        else:
            header = ""
            body_rows = rows[sep_idx + 1:]
    else:
        # 制表符分隔的 Excel 提取没有分隔行，首行通常是列名
        header = rows[0] if len(rows) >= 3 else ""
        body_rows = rows[1:] if header else rows

    if not body_rows:
        return [header] if header else []

    budget = max(chunk_size - prefix_len - len(header) - 2, 64)
    chunks: list[str] = []
    current: list[str] = []

    def emit(group: list[str]) -> None:
        chunks.append((header + "\n" if header else "") + "\n".join(group))

    for row in body_rows:
        if len(row) > budget and not current:
            chunks.extend(_split_oversized_row(row, header, prefix_len, chunk_size))
            continue
        size = sum(len(r) + 1 for r in current)
        if current and size + len(row) > budget:
            emit(current)
            current = [row]
        else:
            current.append(row)
    if current:
        emit(current)
    return chunks


def _chunk_section(
    body: str, prefix: str, chunk_size: int, chunk_overlap: int
) -> list[str]:
    """章节内分块；表格块不参与 overlap，正文块保持原有的重叠行为"""
    body_budget = max(chunk_size - len(prefix), chunk_size // 2)
    items: list[tuple[str, bool]] = []  # (正文, 是否表格)
    text_buf = ""

    def flush_text() -> None:
        nonlocal text_buf
        if text_buf.strip():
            items.extend((c, False) for c in _split_text_chunks(text_buf, body_budget))
        text_buf = ""

    for kind, lines in _split_blocks(body):
        if kind == "table":
            flush_text()
            items.extend(
                (c, True) for c in _chunk_table(lines, len(prefix), chunk_size)
            )
        else:
            para = "\n".join(lines).strip()
            if not para:
                continue
            text_buf = f"{text_buf}\n\n{para}" if text_buf else para
    flush_text()

    result: list[str] = []
    prev_text = ""
    for text, is_table in items:
        if is_table:
            result.append(prefix + text)
            prev_text = ""
            continue
        original = text
        if prev_text and chunk_overlap > 0 and len(prev_text) > chunk_overlap:
            text = prev_text[-chunk_overlap:] + "\n\n" + text
        result.append(prefix + text)
        prev_text = original
    return result


def _is_table_chunk(chunk: str) -> bool:
    for line in chunk.splitlines():
        s = line.strip()
        if s and ("\t" in line or "|" in line):
            return True
    return False


def _chunk_group(chunk: str) -> str:
    """chunk 前缀的顶级章节（第八章 / 一、），用于限制小 chunk 合并范围"""
    m = re.match(
        r"^\[(第[一二三四五六七八九十百零0-9]+[章节部分卷篇]|[一二三四五六七八九十]+、)",
        chunk,
    )
    return m.group(1) if m else ""


def _chunk_body_text(chunk: str) -> str:
    """去掉 chunk 的 ``[章节]`` 前缀，只留正文"""
    if chunk.startswith("["):
        end = chunk.find("]\n")
        if end != -1:
            return chunk[end + 2:]
    return chunk


def _drop_duplicate_chunks(chunks: list[str]) -> list[str]:
    """丢掉被相邻 chunk 完整包含的重复块。

    Excel/Word 提取里常出现「表标题」单独一行、下一行才是表格本身：
    分块后会产生一个 20 字的标题块，BM25 对短文档天然打高分，导致
    “攻防演习值班人名单”命中 2026年HVV值班表 这种标题噪音。
    """
    kept: list[str] = []
    for i, chunk in enumerate(chunks):
        body = _chunk_body_text(chunk).strip()
        if len(body) >= 8:
            prev = _chunk_body_text(chunks[i - 1]).strip() if i > 0 else ""
            nxt = _chunk_body_text(chunks[i + 1]).strip() if i + 1 < len(chunks) else ""
            if body in prev or body in nxt:
                continue
        kept.append(chunk)
    return kept


def _absorb_tiny_chunks(chunks: list[str], min_len: int = 60) -> list[str]:
    """把过短的正文块并进相邻块，避免短块靠长度优势抢占排名。

    只合并正文块：表格块即使很短也保留，因为表头/列名本身是检索上下文。
    """
    out: list[str] = []
    for chunk in chunks:
        body = _chunk_body_text(chunk).strip()
        if len(body) < min_len and out and not _is_table_chunk(chunk):
            out[-1] = out[-1] + "\n\n" + chunk
        else:
            out.append(chunk)
    if (
        len(out) > 1
        and len(_chunk_body_text(out[0]).strip()) < min_len
        and not _is_table_chunk(out[0])
    ):
        out[1] = out[0] + "\n\n" + out[1]
        out.pop(0)
    return out


def _merge_small_chunks(chunks: list[str], chunk_size: int) -> list[str]:
    """合并同一顶级章节下的相邻小文本块。

    技术方案类 Word 文档的 5.1.1/5.1.2 小节常常只有几十个字，一节一个
    chunk 会让 500 字的窗口大部分空着，也稀释检索名额。这里只在同一
    章（如“第八章”）内合并，且绝不合并表格块，避免丢失表头结构。
    """
    out: list[str] = []
    for chunk in chunks:
        if out:
            prev = out[-1]
            group_prev = _chunk_group(prev)
            group_cur = _chunk_group(chunk)
            if (
                group_prev
                and group_prev == group_cur
                and not _is_table_chunk(prev)
                and not _is_table_chunk(chunk)
                and len(prev) + len(chunk) + 2 <= chunk_size
            ):
                out[-1] = prev + "\n\n" + chunk
                continue
        out.append(chunk)
    return out


def _chunk_document(
    text: str,
    file_name: str,
    file_type: DocFileType | None = None,
    *,
    is_url: bool = False,
) -> list[str]:
    """分块并保证结果非空。

    上传、URL 导入、单文档重建三条路径都必须走这里：此前只有上传路径
    做了空内容校验，另外两条会静默把文档标成 ready/0 chunk，用户只看到
    “索引成功”但永远搜不到内容。
    """
    chunks = _chunk_text(text)
    if chunks:
        return chunks

    if is_url:
        raise ValueError("网页内容为空，可能无法访问、需要登录或返回空页面")
    hint = ""
    if file_type == DocFileType.PDF or Path(file_name).suffix.lower() == ".pdf":
        hint = (
            "（PDF 可能为图片扫描件；OCR 未识别到文字。请确认已安装 "
            "rapidocr-onnxruntime 和 pypdfium2，或换清晰度更高的扫描件）"
        )
    raise ValueError(f"文档内容为空: {file_name}{hint}")


def _chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 128,
) -> list[str]:
    """结构化分块：按章节切分，表格保留表头，正文段落优先。

    旧实现只按 ``\\n\\n`` 和字数切，实测周报的表格被切成 1–9 号无表头的
    裸行，模型拿到「王加聪」也不知道是哪个班次。现在：
    - 章节标题（Markdown # / 整行加粗 / Excel Sheet）作为 chunk 前缀；
    - 连续表格行整体处理，表头在每块里重复；
    - 超长表格行只拆最后一列，保留前面的标识列。
    """
    chunks: list[str] = []
    for title, body in _split_sections(text):
        prefix = f"[{title}]\n" if title else ""
        chunks.extend(_chunk_section(body, prefix, chunk_size, chunk_overlap))

    # 丢弃无有效检索字符的 chunk（纯 markdown 表格边框线、分隔符、空白）。
    # 实测某份周报 15 个 chunk 里有 3 个是这种，检索命中它们等于白占名额。
    # 放在这里而不是 _persist_chunks：所有调用方拿到的就是干净列表，
    # _mark_doc_ready(doc_id, len(chunks)) 的 chunk_count 才不会算多。
    chunks = [c for c in chunks if not _is_degenerate_chunk(c)]
    chunks = _drop_duplicate_chunks(chunks)
    chunks = _merge_small_chunks(chunks, chunk_size)
    return _absorb_tiny_chunks(chunks)


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


# 查询侧停用词：疑问词、助词、量词和“现在/还剩”这类口语填充词。
# 只影响查询，不影响索引，避免误删正文里的实词。
_QUERY_STOPWORDS = frozenset(
    {
        "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "么", "是", "在", "有",
        "和", "与", "及", "或", "对", "从", "到", "为", "把", "被", "让", "就",
        "都", "也", "还", "又", "很", "最", "更", "不", "没", "无", "这", "那",
        "哪", "哪个", "哪些", "哪家", "哪里", "什么", "怎么", "怎样", "如何",
        "多少", "几个", "个", "条", "件", "项", "次", "位", "名", "里", "中",
        "上", "下", "前", "后", "内", "外", "时", "时候", "请问", "一下", "帮我",
        "查", "找", "看", "请", "给", "我", "你", "他", "她", "它", "们",
        "关于", "有没有", "是否", "需要", "可以", "能", "会", "要", "现在",
        "还有", "还剩", "剩", "几台", "几", "台",
    }
)


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
    # 15→12：目标漏召回在 rank 11，12 仍能覆盖；同时把 qwen3.7 的一次
    # 精排耗时压到 1.5s 目标附近。
    RERANK_CANDIDATES = 12

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
        reranker_api_style: str | None = None,
        reranker_timeout_s: float | None = None,
        reranker_collections: str | None = None,
        vector_backend: str | None = None,
        onnx_model_dir: str | None = None,
        endpoints_config_path: Path | None = None,
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
        self._onnx_model_dir_override = onnx_model_dir or ""
        self._vector_backend_pref = (
            vector_backend or _settings_value("kb_vector_backend", "auto")
        ).strip().lower()
        if self._vector_backend_pref not in {"off", "auto", "onnx", "chromadb"}:
            logger.warning(
                f"[KnowledgeManager] 未知向量后端 {self._vector_backend_pref!r}，按 auto 处理"
            )
            self._vector_backend_pref = "auto"

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
        self._reranker_api_style = reranker_api_style or _settings_value(
            "kb_reranker_api_style", "auto"
        )
        self._reranker_timeout_s = (
            reranker_timeout_s
            if reranker_timeout_s is not None
            else float(_settings_value("kb_reranker_timeout_s", "8"))
        )
        collections_raw = (
            reranker_collections
            if reranker_collections is not None
            else _settings_value("kb_reranker_collections", "")
        )
        self._reranker_collections = {
            item.strip() for item in str(collections_raw).split(",") if item.strip()
        }

        # UI 配置的知识库端点优先于 .env；未配置时保持 .env 回退。
        self._knowledge_endpoint = self._load_knowledge_endpoint(endpoints_config_path)
        self._knowledge_endpoint_name = ""
        self._apply_knowledge_endpoint(self._knowledge_endpoint)

        # 开关三态：显式 True/False 照办；未设置时只在配了在线 Rerank API 时启用。
        # 本地模型约 1.1GB 且常驻内存，不在默认路径上主动触发下载。
        enabled = reranker_enabled
        if enabled is None:
            enabled = _settings_opt_bool("kb_reranker_enabled")
        if enabled is None:
            enabled = bool(self._reranker_api_base and self._reranker_api_key)
        self.reranker_enabled = bool(enabled)
        if self.reranker_enabled and self._reranker_api_base and not self._reranker_collections:
            logger.warning(
                "[KnowledgeManager] 在线精排已配置但 KB_RERANKER_COLLECTIONS 为空，"
                "不会调用在线 API（防止私有集合内容外发）"
            )

        # 向量后端状态（chromadb 或轻量 ONNX）
        self._chroma_client = None
        self._chroma_collection = None
        self._embedding_model = None
        self._vector_enabled = False
        self._vector_backend = ""          # "" | "chromadb" | "onnx"
        self._onnx_model_id = ""           # 模型指纹，切换时重建向量
        self._onnx_vector_cache: dict[str, tuple[Any, list[str]]] = {}
        self._onnx_cache_lock = threading.RLock()
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

        # 后台初始化向量后端（ONNX / ChromaDB / 纯 FTS5）
        self._start_vector_init()

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
                title,
                tokenize='unicode61'
            );

            CREATE INDEX IF NOT EXISTS idx_kb_docs_collection
                ON kb_documents(collection_id);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc
                ON kb_chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_collection
                ON kb_chunks(collection_id);

            -- 轻量 ONNX 向量：直接存 SQLite BLOB，避免引入 ChromaDB。
            -- 向量在内存中按 collection 聚合，单库规模 5 万 chunk 以内足够。
            CREATE TABLE IF NOT EXISTS kb_chunk_vectors (
                chunk_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                model TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb_vectors_collection
                ON kb_chunk_vectors(collection_id);

            CREATE TABLE IF NOT EXISTS kb_vector_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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
        self._migrate_fts_title(conn)
        conn.commit()
        conn.close()
        logger.info(f"[KnowledgeManager] SQLite 初始化完成: {self._db_path}")

    def _migrate_fts_title(self, conn: sqlite3.Connection) -> None:
        """把旧版只有 content 列的 FTS 表升级为 content + title。

        文件名以前不进索引，用户按文档名/项目名搜不到。新增 title 列后
        ``bm25`` 可以给文件名更高的权重。旧表无法 ALTER 加列，只能重建；
        重建数据直接来自 kb_chunks + kb_documents，不需要重新解析原文件。
        """
        cols = [row[1] for row in conn.execute("PRAGMA table_info(kb_chunks_fts)")]
        if "title" in cols:
            return

        logger.info("[KnowledgeManager] 升级 FTS 索引：新增 title 列并重建")
        conn.execute("DROP TABLE IF EXISTS kb_chunks_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE kb_chunks_fts USING fts5("
            "content, title, tokenize='unicode61')"
        )
        rows = conn.execute(
            "SELECT c.rowid AS rid, c.content AS content, d.filename AS filename "
            "FROM kb_chunks c LEFT JOIN kb_documents d ON d.id = c.doc_id"
        ).fetchall()
        for rid, content, filename in rows:
            conn.execute(
                "INSERT INTO kb_chunks_fts (rowid, content, title) VALUES (?, ?, ?)",
                (
                    rid,
                    self._tokenize_for_fts(content or ""),
                    self._tokenize_for_fts(filename or ""),
                ),
            )
        logger.info(f"[KnowledgeManager] FTS 重建完成: {len(rows)} chunks")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── 向量后端初始化 ──────────────────────────────────────────────────────

    def _start_vector_init(self) -> None:
        """后台选择并初始化向量后端（不阻塞启动）"""
        with self._init_lock:
            if self._init_started:
                return
            self._init_started = True

        threading.Thread(
            target=self._do_vector_init,
            name="KnowledgeManager-vector-init",
            daemon=True,
        ).start()

    def _do_vector_init(self) -> None:
        pref = self._vector_backend_pref
        if pref == "off":
            logger.info("[KnowledgeManager] 向量后端已关闭，使用纯 FTS5 模式")
            return

        if pref in {"auto", "onnx"}:
            try:
                self._do_onnx_init()
                return
            except Exception as e:
                logger.warning(f"[KnowledgeManager] ONNX 向量后端不可用: {e}")
                if pref == "onnx":
                    logger.warning(
                        "[KnowledgeManager] 已强制 onnx 但初始化失败，降级纯 FTS5"
                    )
                    return

        if pref in {"auto", "chromadb"}:
            self._do_chroma_init()

    def _load_knowledge_endpoint(
        self, endpoints_config_path: Path | None = None
    ) -> dict | None:
        """读取 data/llm_endpoints.json 中第一条启用的知识库端点"""
        path = (
            Path(endpoints_config_path)
            if endpoints_config_path is not None
            else self.data_dir.parent / "llm_endpoints.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"[KnowledgeManager] 读取知识库端点配置失败 {path}: {e}")
            return None

        endpoints = data.get("knowledge_endpoints") or []
        if not isinstance(endpoints, list):
            return None
        for ep in sorted(
            [e for e in endpoints if isinstance(e, dict)],
            key=lambda e: (int(e.get("priority", 999) or 999), str(e.get("name", ""))),
        ):
            if ep.get("enabled", True) is False:
                continue
            if str(ep.get("base_url") or "").strip():
                return ep
        return None

    def _apply_knowledge_endpoint(self, endpoint: dict | None) -> None:
        if not endpoint:
            return
        base = str(endpoint.get("base_url") or "").strip()
        model = str(endpoint.get("model") or "").strip()
        if base:
            self._reranker_api_base = base
        if model:
            self._reranker_api_model = model

        extra = endpoint.get("extra_params") or {}
        style = str(extra.get("style") or endpoint.get("api_type") or "auto").strip()
        if style == "rerank":
            style = "auto"
        self._reranker_api_style = style or "auto"

        timeout = extra.get("timeout", endpoint.get("timeout"))
        if timeout is not None:
            try:
                self._reranker_timeout_s = float(timeout)
            except (TypeError, ValueError):
                pass

        key_env = str(endpoint.get("api_key_env") or "").strip()
        key = str(endpoint.get("api_key") or "").strip() or self._read_env_value(key_env)
        if key:
            self._reranker_api_key = key
        elif self._reranker_api_key:
            logger.warning(
                f"[KnowledgeManager] 知识库端点 {endpoint.get('name')} 未找到 API Key"
                f"（{key_env or '未配置 api_key_env'}），回退到 .env 中的 Reranker Key"
            )
        self._knowledge_endpoint_name = str(endpoint.get("name") or "").strip()
        logger.info(
            f"[KnowledgeManager] 使用知识库端点: {self._knowledge_endpoint_name} "
            f"({self._reranker_api_model}, style={self._reranker_api_style})"
        )

    def _read_env_value(self, name: str) -> str:
        """读取动态命名的 API Key 环境变量（先 os.environ，再工作区 .env）"""
        if not name:
            return ""
        value = os.environ.get(name, "")
        if value:
            return value
        try:
            from mclaw.utils.env_config import read_env_file

            env_path = self.data_dir.parent.parent / ".env"
            return read_env_file(env_path).get(name, "")
        except Exception:
            return ""

    def _resolve_onnx_model_dir(self) -> Path:
        from .onnx_embedder import DEFAULT_MODEL_DIR_NAME, _find_model_file

        raw = self._onnx_model_dir_override or _settings_value("kb_onnx_model_dir", "")
        if raw:
            path = Path(raw).expanduser()
            if path.is_absolute():
                return path
            try:
                from mclaw.config import settings as _settings

                root = Path(getattr(_settings, "project_root", Path.cwd()))
            except Exception:
                root = Path.cwd()
            return root / path

        # 默认位置：先找随安装包分发的 vector-lite 模块模型，再回退到
        # 工作区 data/knowledge/models（首次下载会写到那里）。
        default_dir = self.data_dir / "models" / DEFAULT_MODEL_DIR_NAME
        for candidate in self._onnx_model_dir_candidates(DEFAULT_MODEL_DIR_NAME):
            if (candidate / "tokenizer.json").is_file() and _find_model_file(candidate):
                return candidate
        return default_dir

    @staticmethod
    def _onnx_model_dir_candidates(model_dir_name: str) -> list[Path]:
        """已安装的 vector-lite 模块可能自带模型（离线客户机）。"""
        candidates: list[Path] = []
        try:
            from mclaw.config import settings as _settings

            home = Path(_settings.mclaw_home)
        except Exception:
            home = Path.home() / ".mclaw"
        candidates.append(home / "modules" / "vector-lite" / "models" / model_dir_name)

        try:
            from mclaw.runtime_env import get_bootstrap_manifest_path

            manifest = get_bootstrap_manifest_path()
            if manifest is not None:
                resources = Path(manifest).parent.parent
                candidates.append(
                    resources / "modules" / "vector-lite" / "models" / model_dir_name
                )
        except Exception:
            pass
        return candidates

    def _do_onnx_init(self) -> None:
        """初始化轻量 ONNX 向量后端（不依赖 ChromaDB / torch）"""
        from .onnx_embedder import OnnxEmbedder

        model_dir = self._resolve_onnx_model_dir()
        ok, reason = OnnxEmbedder.is_available(model_dir)
        if not ok and self._vector_backend_pref == "onnx":
            # 显式启用 onnx 时允许首次下载（客户机可联网）；auto 模式不擅自下载
            logger.info(f"[KnowledgeManager] ONNX 模型缺失，尝试下载: {reason}")
            ok, reason = OnnxEmbedder.ensure_model(model_dir)
        if not ok:
            raise RuntimeError(f"{reason}（模型目录: {model_dir}）")

        embedder = OnnxEmbedder(model_dir)
        model_id = f"onnx:{embedder.model_file}:{embedder.dimension}"

        with self._init_lock:
            self._embedding_model = embedder
            self._onnx_model_id = model_id
            self._vector_enabled = True
            self._vector_backend = "onnx"

        logger.info(
            f"[KnowledgeManager] ONNX 向量模式就绪 "
            f"(model={model_id}, dir={model_dir})"
        )
        self._prepare_onnx_vector_store(model_id)

    def _prepare_onnx_vector_store(self, model_id: str) -> None:
        """模型指纹校验 + 缺失向量的后台补建"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM kb_vector_meta WHERE key = 'model'"
            ).fetchone()
            old_model = row["value"] if row else ""
            should_reindex = False
            if old_model and old_model != model_id:
                logger.warning(
                    f"[KnowledgeManager] ONNX 模型切换 {old_model} → {model_id}，"
                    "清空旧向量并重建"
                )
                conn.execute("DELETE FROM kb_chunk_vectors")
                should_reindex = True
            conn.execute(
                "INSERT OR REPLACE INTO kb_vector_meta (key, value) VALUES ('model', ?)",
                (model_id,),
            )
            if not should_reindex:
                missing = conn.execute(
                    "SELECT COUNT(*) FROM kb_documents d "
                    "WHERE d.status = 'ready' AND NOT EXISTS ("
                    "  SELECT 1 FROM kb_chunk_vectors v "
                    "  JOIN kb_chunks c ON c.id = v.chunk_id "
                    "  WHERE c.doc_id = d.id AND v.model = ?"
                    ")",
                    (model_id,),
                ).fetchone()[0]
                should_reindex = missing > 0
            conn.commit()
        finally:
            conn.close()

        if should_reindex:
            self.schedule_reindex_all(reason="ONNX 向量初始化/补建")

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
                    api_style=self._reranker_api_style,
                    timeout=self._reranker_timeout_s,
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
                self._vector_backend = "chromadb"

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
        conn.execute("DELETE FROM kb_chunk_vectors WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_chunks_fts WHERE rowid NOT IN (SELECT rowid FROM kb_chunks)")
        conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()
        with self._onnx_cache_lock:
            self._onnx_vector_cache.clear()

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
            "SELECT * FROM kb_documents WHERE collection_id = ? AND file_path = ?",
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
            # 删除旧 chunks 和旧向量
            conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc.id,))
            conn.execute("DELETE FROM kb_chunk_vectors WHERE doc_id = ?", (doc.id,))
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
        # 先分块并校验非空，再写文档记录：空页面不能留下 ready/0 chunk 的
        # 假成功记录。
        chunks = _chunk_document(text, url, is_url=True)
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
             doc.created_at, doc.updated_at, json.dumps(doc.metadata, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

        # Index chunks from extracted text
        self._persist_chunks(doc, chunks)
        self._embed_chunks(doc, chunks)

        self._mark_doc_ready(doc.id, len(chunks))
        return self.get_document(doc.id) or doc

    def _index_document(self, doc: KnowledgeDocument, file_path: Path, file_type: DocFileType) -> None:
        """执行实际的文档索引"""
        self._mark_doc_status(doc.id, DocStatus.INDEXING)

        text = _extract_text(file_path, file_type)
        chunks = _chunk_document(text, file_path.name, file_type)

        self._persist_chunks(doc, chunks)
        self._embed_chunks(doc, chunks)
        self._mark_doc_ready(doc.id, len(chunks))

        logger.info(
            f"[KnowledgeManager] 索引完成: {doc.filename} → {len(chunks)} chunks"
        )

    def _persist_chunks(self, doc: KnowledgeDocument, chunks: list[str]) -> None:
        """将 chunks 写入 SQLite + FTS5"""
        conn = self._get_conn()
        tokenized_title = self._tokenize_for_fts(doc.filename)
        for i, text in enumerate(chunks):
            chunk_id = _short_uuid()
            token_count = _estimate_tokens(text)
            conn.execute(
                "INSERT INTO kb_chunks (id, doc_id, collection_id, content, chunk_index, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk_id, doc.id, doc.collection_id, text, i, token_count),
            )
            # 同步写入 FTS5（预分词）。title 让“按文档名找资料”也能命中，
            # 查询侧用 bm25 的列权重把标题命中排到正文命中之前。
            tokenized = self._tokenize_for_fts(text)
            conn.execute(
                "INSERT INTO kb_chunks_fts (rowid, content, title) VALUES ("
                "  (SELECT rowid FROM kb_chunks WHERE id = ?), ?, ?"
                ")",
                (chunk_id, tokenized, tokenized_title),
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

        if self._vector_backend == "onnx":
            try:
                import numpy as np

                vectors = self._embedding_model.encode(texts, batch_size=32)
                for cid, vec in zip(chunk_ids, vectors):
                    conn.execute(
                        "INSERT OR REPLACE INTO kb_chunk_vectors "
                        "(chunk_id, doc_id, collection_id, model, dimension, vector) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            cid,
                            doc.id,
                            doc.collection_id,
                            self._onnx_model_id,
                            len(vec),
                            np.asarray(vec, dtype=np.float32).tobytes(),
                        ),
                    )
                conn.commit()
                with self._onnx_cache_lock:
                    self._onnx_vector_cache.pop(doc.collection_id, None)
                    self._onnx_vector_cache.pop("__all__", None)
                logger.info(
                    f"[KnowledgeManager] ONNX 向量化完成: {doc.filename} → "
                    f"{len(vectors)} vectors"
                )
            except Exception as e:
                logger.warning(f"[KnowledgeManager] ONNX 向量化失败: {e}")
            finally:
                conn.close()
            return

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

        try:
            if doc.file_type == DocFileType.URL.value:
                url = doc.metadata.get("source_url", "")
                if not url:
                    raise ValueError("URL 文档缺少 source_url")
                text = _extract_text_from_url(url)
                chunks = _chunk_document(text, url, is_url=True)
            elif doc.file_path:
                file_path = Path(doc.file_path)
                if not file_path.exists():
                    raise FileNotFoundError(
                        f"文档文件已丢失: {file_path.name}，请删除此记录后重新上传"
                    )
                file_type = (
                    DocFileType(doc.file_type)
                    if doc.file_type in [e.value for e in DocFileType]
                    else DocFileType.TEXT
                )
                text = _extract_text(file_path, file_type)
                chunks = _chunk_document(text, file_path.name, file_type)
            else:
                raise ValueError(f"无法定位文档内容: {doc_id}")
        except Exception as e:
            self._mark_doc_error(doc_id, str(e))
            raise

        # 分块成功后才删除旧 chunks 和向量：重建失败时保留原索引，
        # 至少搜索还能用，不会因为一次重试把文档彻底弄空。
        old_emb = self._get_chunk_embedding_ids(doc_id)
        conn = self._get_conn()
        conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_chunk_vectors WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_chunks_fts WHERE rowid NOT IN (SELECT rowid FROM kb_chunks)")
        conn.commit()
        conn.close()
        with self._onnx_cache_lock:
            self._onnx_vector_cache.clear()
        if old_emb and self._chroma_collection:
            try:
                self._chroma_collection.delete(ids=old_emb)
            except Exception:
                pass

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

        # 清掉已删除/重建失败文档留下的孤儿向量
        conn = self._get_conn()
        try:
            conn.execute(
                "DELETE FROM kb_chunk_vectors "
                "WHERE chunk_id NOT IN (SELECT id FROM kb_chunks)"
            )
            conn.commit()
        finally:
            conn.close()
        with self._onnx_cache_lock:
            self._onnx_vector_cache.clear()

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
                "vector_backend": self._vector_backend,
                "onnx_model": self._onnx_model_id,
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
        # ONNX 向量的实测最佳权重是 BM25:向量 = 1:0.5（38 条评测集：
        # Hit@8 0.895→0.921，无类别倒退）；ChromaDB 沿用等权。
        if self._vector_backend == "onnx":
            merged = self._rrf_fusion(
                bm25_results, vector_results, k=60,
                bm25_weight=1.0, vector_weight=0.5,
            )
        else:
            merged = self._rrf_fusion(bm25_results, vector_results, k=60)
        merged.sort(key=lambda x: x.score, reverse=True)

        # Reranker 精排：对 RRF 融合后的候选逐对精细打分。
        # 候选数刻意压到 15：cross-encoder 是 O(n) 次前向，CPU 上每对百毫秒级，
        # 30 条会让单次检索多出数秒，而精排收益集中在头几条。
        reranker = self._reranker
        if reranker is not None and reranker.available and len(merged) > 1:
            use_api = bool(getattr(reranker, "use_api", False))
            if use_api and not self._rerank_allowed(collection_id):
                logger.debug(
                    f"[Reranker] 在线精排跳过：collection={collection_id} 不在白名单"
                )
            else:
                candidate_count = min(self.RERANK_CANDIDATES, len(merged))
                candidates = merged[:candidate_count]
                if use_api:
                    # 审计：只记录集合与条数，不记录正文
                    logger.info(
                        f"[Reranker] 在线精排: collection={collection_id} "
                        f"candidates={candidate_count}"
                    )
                reranked = self._rerank_results(query, candidates, reranker)
                if reranked is not None:
                    return reranked[:top_k]

        return merged[:top_k]

    def _rerank_allowed(self, collection_id: str | None) -> bool:
        """在线精排白名单校验。

        在线 API 会把 query + top 候选正文发出去，因此必须显式配置允许的
        collection；未指定 collection（跨库搜索）一律不调用。
        """
        if not self._reranker_collections or not collection_id:
            return False
        if collection_id in self._reranker_collections:
            return True
        coll = self.get_collection(collection_id)
        return bool(coll and coll.name in self._reranker_collections)

    def _rerank_results(
        self,
        query: str,
        candidates: list[SearchResult],
        reranker,
    ) -> list[SearchResult] | None:
        """用 reranker 对候选结果精排；失败返回 None（沿用原排序）"""
        chunk_ids = [r.chunk.id for r in candidates]
        texts = self._fetch_rerank_texts(chunk_ids)
        if len(texts) != len(candidates):
            # 部分 chunk 已被删除等异常情况，放弃精排
            return None

        try:
            scores = reranker.rerank(query, [texts[cid] for cid in chunk_ids])
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

    def _fetch_rerank_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """批量读取精排文本：正文前带上文件名。

        周报/评估表模板之间的正文高度相似，只送正文时 cross-encoder 很容易
        把“评估表模板”排到“盘龙法院评估表”前面；文件名是区分项目最直接
        的信号，必须一起送。
        """
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT c.id, c.content, d.filename AS filename "
                "FROM kb_chunks c LEFT JOIN kb_documents d ON d.id = c.doc_id "
                f"WHERE c.id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
            out: dict[str, str] = {}
            for r in rows:
                filename = (r["filename"] or "").strip()
                prefix = f"【文档：{filename}】\n" if filename else ""
                out[r["id"]] = prefix + (r["content"] or "")
            return out
        except Exception:
            return {}
        finally:
            conn.close()

    @staticmethod
    def _sanitize_fts_query(query: str, joiner: str = " OR ") -> str:
        """Make user input safe for FTS5 MATCH."""
        # 除 FTS5 的查询语法字符外，日期/型号里的 . - / 也会触发
        # "fts5: syntax error near ."；统一替换成空格（unicode61 本来也
        # 会在这些字符处断词，不会损失检索能力）。
        special = set('"*(){}[]^~:.,/\\?')
        cleaned = "".join(c if c not in special else " " for c in query)
        tokens = cleaned.split()
        if not tokens:
            return '""'
        return joiner.join(tokens)

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

    @staticmethod
    def _filter_query_tokens(segmented: str) -> list[str]:
        return [
            t
            for t in segmented.split()
            if t
            and t.lower() not in _QUERY_STOPWORDS
            and t.upper() not in {"AND", "OR", "NOT", "NEAR"}
        ]

    @staticmethod
    def _query_tokens(text: str) -> str:
        """查询侧精确分词 + 去停用词，用于 AND 查询。

        注意不要用 cut_for_search：它会把“防火墙”拆成“防火/火墙/防火墙”
        三个 token，OR 查询里等于给这个词三倍权重，反而把真正含“库存”的
        设备台账压到第 10 名以后。
        """
        try:
            import jieba

            parts = [t for t in jieba.cut(text) if t.strip()]
        except ImportError:
            parts = text.split()
        tokens = KnowledgeManager._filter_query_tokens(" ".join(parts))
        return " ".join(tokens) if tokens else " ".join(parts)

    def _fts_match(
        self, safe_query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        """执行一次 FTS5 MATCH，返回 chunk_id -> 分数（越大越相关）"""
        conn = self._get_conn()
        result: dict[str, float] = {}
        try:
            if collection_id:
                rows = conn.execute(
                    "SELECT c.id, c.content, bm25(kb_chunks_fts, 1.0, 3.0) AS rank "
                    "FROM kb_chunks_fts f "
                    "JOIN kb_chunks c ON c.rowid = f.rowid "
                    "WHERE kb_chunks_fts MATCH ? AND c.collection_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (safe_query, collection_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.id, bm25(kb_chunks_fts, 1.0, 3.0) AS rank "
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

        # SQLite 的 bm25() 返回负值，越小（越负）越相关。直接取反即可，
        # 不要用 1/(1+rank)：rank 在 (-2, -1) 区间时分母过零，分数会在
        # 巨大正数和负数之间跳变，导致候选排序不再单调（实测会把真正
        # 命中的长 chunk 排到十几名开外）。
        for r in rows:
            rank = r["rank"] if isinstance(r, sqlite3.Row) else r[1]
            chunk_id = r["id"] if isinstance(r, sqlite3.Row) else r[0]
            result[chunk_id] = -rank
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
        """FTS5 + jieba BM25 搜索：AND 优先、OR 补召回，同义词再补一层。

        纯 OR 查询会被高频词带偏：问“信舷防火墙还有多少库存”时，所有供应商
        文档都含“防火墙”，真正含“库存”的设备台账反而排到第 10 名开外。
        现在先用精确分词做 AND（要求关键实体共现），AND 结果不够时再用
        cut_for_search 的 OR 结果补齐，并让 OR 结果排在 AND 结果之后。
        """
        and_query = self._sanitize_fts_query(self._query_tokens(query), " AND ")
        or_query = self._sanitize_fts_query(
            " ".join(self._filter_query_tokens(self._segment(query))), " OR "
        )

        and_scores = (
            self._fts_match(and_query, collection_id, limit)
            if " AND " in and_query
            else {}
        )
        or_scores = self._fts_match(or_query, collection_id, limit)

        if and_scores:
            # weight < 1 保证只靠 OR 命中的 chunk 排在 AND 命中之后
            result = self._merge_expansion(and_scores, or_scores, weight=0.5)
        else:
            result = or_scores

        extras = self._expansion_terms(query)
        if extras:
            expanded = self._fts_match(
                self._sanitize_fts_query(" ".join(extras)), collection_id, limit
            )
            if expanded:
                logger.debug(f"[KnowledgeManager] 查询扩展补召回: +{len(extras)} 词")
                result = self._merge_expansion(result, expanded)
        return result

    def _load_onnx_vectors(
        self, collection_id: str | None
    ) -> tuple[Any, list[str]]:
        """加载并缓存某 collection 的全部 ONNX 向量（内存余弦检索）"""
        cache_key = collection_id or "__all__"
        with self._onnx_cache_lock:
            cached = self._onnx_vector_cache.get(cache_key)
            if cached is not None:
                return cached

        import numpy as np

        conn = self._get_conn()
        try:
            if collection_id:
                rows = conn.execute(
                    "SELECT chunk_id, vector FROM kb_chunk_vectors "
                    "WHERE collection_id = ? AND model = ?",
                    (collection_id, self._onnx_model_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT chunk_id, vector FROM kb_chunk_vectors WHERE model = ?",
                    (self._onnx_model_id,),
                ).fetchall()
        finally:
            conn.close()

        ids = [r["chunk_id"] for r in rows]
        if rows:
            matrix = np.vstack(
                [np.frombuffer(r["vector"], dtype=np.float32) for r in rows]
            ).astype(np.float32)
        else:
            dim = getattr(self._embedding_model, "dimension", 0) or 0
            matrix = np.zeros((0, dim), dtype=np.float32)

        with self._onnx_cache_lock:
            self._onnx_vector_cache[cache_key] = (matrix, ids)
        return matrix, ids

    def _search_vector_onnx(
        self, query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        if not self._vector_enabled or self._embedding_model is None:
            return {}
        try:
            import numpy as np

            q = self._embedding_model.encode([query])[0]
            matrix, ids = self._load_onnx_vectors(collection_id)
            if not ids:
                return {}
            sims = matrix @ np.asarray(q, dtype=np.float32)
            top = np.argsort(-sims)[:limit]
            return {ids[i]: float(sims[i]) for i in top}
        except Exception as e:
            logger.warning(f"[KnowledgeManager] ONNX 向量搜索失败: {e}")
            return {}

    def _search_vector(
        self, query: str, collection_id: str | None, limit: int
    ) -> dict[str, float]:
        """向量搜索：ONNX 内存余弦 / ChromaDB"""
        if self._vector_backend == "onnx":
            return self._search_vector_onnx(query, collection_id, limit)
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
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> list[SearchResult]:
        """加权 Reciprocal Rank Fusion 融合 BM25 和向量排名"""
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
            rrf_score = (
                bm25_weight / (k + bm25_rank)
                + vector_weight / (k + vector_rank)
            )

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
