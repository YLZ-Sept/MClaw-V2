#!/usr/bin/env python3
"""把采集结果文件摄入知识库集合（默认「招投标采集结果」）。

由招投标小助手在每日采集后调用，把中标信息 / 采购意向统计写入的文件
摄入知识库，供后续检索与历史比对。

用法:
    python scripts/kb_ingest_collected.py <文件路径> [集合名]

- 文件必须是知识库支持的类型（.md / .txt / .docx / .xlsx 等）。
- 不传集合名时，默认摄入「招投标采集结果」集合。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DEFAULT_COLLECTION = "招投标采集结果"


def resolve_collection_id(data_dir: Path, name: str) -> str | None:
    db = data_dir / "knowledge.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT id FROM kb_collections WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/kb_ingest_collected.py <文件路径> [集合名]")
        return 1

    file_path = Path(sys.argv[1]).resolve()
    coll_name = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_COLLECTION

    if not file_path.exists():
        print(f"[失败] 文件不存在: {file_path}")
        return 1

    project_root = Path(__file__).resolve().parent.parent
    data_dir = project_root / "data" / "knowledge"

    coll_id = resolve_collection_id(data_dir, coll_name)
    if not coll_id:
        print(f"[失败] 未找到集合「{coll_name}」")
        return 1

    from mclaw.memory.knowledge_manager import KnowledgeManager

    km = KnowledgeManager(data_dir=data_dir)
    doc = km.ingest_file(
        collection_id=coll_id,
        file_path=file_path,
        uploaded_by="zhaobiao-assistant",
    )
    count = getattr(doc, "chunk_count", "?")
    print(f"[成功] 已摄入 {file_path.name} -> 集合「{coll_name}」({coll_id}), chunks={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
