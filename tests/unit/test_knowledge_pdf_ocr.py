"""扫描件 PDF OCR 测试（2026-11-09）

覆盖三个回归点：
1. 只有文字过少的页才走 OCR，文字页保持原文（混合型 PDF 不再整份丢字）。
2. OCR 依赖缺失/初始化失败时降级为空结果，不阻断其他格式的导入。
3. 真实图片 PDF 能被 RapidOCR 识别（依赖未安装的机器自动跳过）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mclaw.memory import knowledge_manager as km


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdf:
    def __init__(self, texts: list[str]) -> None:
        self.pages = [_FakePage(t) for t in texts]

    def __enter__(self) -> _FakePdf:
        return self

    def __exit__(self, *args) -> bool:
        return False


def _patch_pdfplumber(monkeypatch, page_texts: list[str]) -> None:
    fake = types.SimpleNamespace(open=lambda _path: _FakePdf(page_texts))
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)


def test_only_low_text_pages_go_through_ocr(monkeypatch):
    texts = ["第一页是正常提取的正文内容" * 3, "", "第三页只有图片"]
    _patch_pdfplumber(monkeypatch, texts)

    seen_pages: list[int] = []

    def fake_ocr(file_path, page_indexes, max_pages):
        seen_pages.extend(page_indexes)
        return {1: "OCR 第二页内容", 2: "OCR 第三页内容"}

    monkeypatch.setattr(km, "_ocr_pdf_pages", fake_ocr)

    result = km._extract_pdf_text(Path("mixed.pdf"))

    # 只 OCR 文字过少的第二、三页
    assert seen_pages == [1, 2]
    assert "第一页是正常提取的正文内容" in result
    assert "OCR 第二页内容" in result
    assert "OCR 第三页内容" in result


def test_ocr_failure_keeps_extractable_text(monkeypatch):
    texts = ["正常正文" * 10, "扫描页"]
    _patch_pdfplumber(monkeypatch, texts)
    # 模拟 OCR 什么都没识别出来（依赖缺失或图片质量差）
    monkeypatch.setattr(km, "_ocr_pdf_pages", lambda *a, **k: {})

    result = km._extract_pdf_text(Path("scan.pdf"))

    assert result.startswith("正常正文")
    assert "扫描页" in result


def test_ocr_missing_dependency_returns_empty(monkeypatch):
    # sys.modules 里的 None 会让 import 直接抛 ImportError
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)

    assert km._ocr_pdf_pages(Path("does-not-exist.pdf"), [0], 10) == {}


def test_ocr_max_pages_env(monkeypatch):
    monkeypatch.setenv("KB_OCR_MAX_PAGES", "7")
    assert km._ocr_max_pages() == 7
    monkeypatch.setenv("KB_OCR_MAX_PAGES", "not-a-number")
    assert km._ocr_max_pages() == km._DEFAULT_OCR_MAX_PAGES


def _find_font() -> str | None:
    candidates = [
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


@pytest.mark.skipif(
    _find_font() is None, reason="没有可用于生成测试图片的字体"
)
def test_scanned_image_pdf_is_recognized(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(_find_font(), 90)
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "KB OCR TEST 2026", fill="black", font=font)
    draw.text((100, 260), "知识库扫描件", fill="black", font=font)

    pdf_path = tmp_path / "scanned.pdf"
    image.save(pdf_path, "PDF", resolution=150)

    text = km._extract_pdf_text(pdf_path)

    assert "OCR" in text.upper()
    assert "2026" in text
