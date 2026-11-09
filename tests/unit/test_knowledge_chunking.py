"""知识库结构化分块测试

覆盖 2026-11 引入的表头保留 / 章节前缀 / 超长行标识列保留行为。
"""

from mclaw.memory.knowledge_manager import _chunk_text, _extract_text
from mclaw.memory.knowledge_types import DocFileType


def test_markdown_table_repeats_header_and_section_title():
    text = """# 项目周报
**一、本周工作重点**

| 序号 | 客户名称 | 项目名称 | 工作内容 |
| --- | --- | --- | --- |
| 1 | 甲公司 | 项目A | 完成渗透测试 |
| 2 | 乙公司 | 项目B | 完成等保测评 |
| 3 | 丙公司 | 项目C | 完成应急演练 |
"""
    chunks = _chunk_text(text, chunk_size=120, chunk_overlap=0)
    table_chunks = [c for c in chunks if "序号" in c]

    assert len(table_chunks) == 2
    for chunk in table_chunks:
        # 每块都带章节上下文和表头，不能是裸数据行
        assert chunk.startswith("[项目周报 / 一、本周工作重点]")
        assert "| 序号 | 客户名称 | 项目名称 | 工作内容 |" in chunk

    joined = "\n".join(table_chunks)
    assert "甲公司" in joined and "丙公司" in joined


def test_oversized_row_keeps_identifier_cells():
    content = "<br>".join(f"第{i}项工作内容说明" for i in range(1, 60))
    text = (
        "**一、本周工作重点**\n\n"
        "| 序号 | 客户名称 | 项目名称 | 工作内容 |\n"
        "| --- | --- | --- | --- |\n"
        f"| 9 | 曲靖市商业银行 | 驻场项目 | {content} |\n"
    )
    chunks = _chunk_text(text, chunk_size=300, chunk_overlap=0)

    assert len(chunks) > 1
    for chunk in chunks:
        # 超长行被拆开后，每一段都必须还能看出属于哪个客户/项目
        assert "曲靖市商业银行" in chunk
        assert "驻场项目" in chunk
        assert "| 序号 | 客户名称 | 项目名称 | 工作内容 |" in chunk


def test_excel_sheet_title_and_header_repeat():
    text = (
        "=== Sheet: 应付账款 ===\n"
        "供应商\t金额\t到期日\n"
        "A公司\t1000\t2026-01-01\n"
        "B公司\t2000\t2026-02-01\n"
        "C公司\t3000\t2026-03-01"
    )
    chunks = _chunk_text(text, chunk_size=60, chunk_overlap=0)

    assert chunks
    for chunk in chunks:
        assert chunk.startswith("[工作表：应付账款]")
        assert "供应商\t金额\t到期日" in chunk
    assert "C公司" in "\n".join(chunks)


def test_plain_text_keeps_overlap_without_section_prefix():
    text = ("甲" * 120) + "\n\n" + ("乙" * 120)
    chunks = _chunk_text(text, chunk_size=200, chunk_overlap=40)

    assert len(chunks) == 2
    assert not chunks[0].startswith("[")
    assert chunks[1].startswith(chunks[0][-40:])


def test_degenerate_chunks_are_dropped():
    assert _chunk_text("|---|---|\n| --- | --- |") == []


def test_docx_extraction_includes_table_cells(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("供应商概况")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "公司名称"
    table.cell(0, 1).text = "优备科技有限公司"
    path = tmp_path / "sample.docx"
    doc.save(path)

    text = _extract_text(path, DocFileType.DOCX)

    # 表格内容不能丢，且用制表符连接，交给分块器识别为表格
    assert "供应商概况" in text
    assert "公司名称\t优备科技有限公司" in text
