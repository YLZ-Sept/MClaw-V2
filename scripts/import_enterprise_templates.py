#!/usr/bin/env python
"""手动把企业运营内置模板导入部门知识库。

首次启动时 ``deploy_system_presets`` 会自动执行同一套导入；本脚本用于
KB manager 未就绪、或需要强制重建模板索引时兜底。

用法:
    python scripts/import_enterprise_templates.py
    python scripts/import_enterprise_templates.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="导入企业运营内置模板到部门知识库")
    parser.add_argument(
        "--force",
        action="store_true",
        help="先删除系统导入的同名旧文档，再重新入库",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="知识库数据目录，默认 <project_root>/data/knowledge",
    )
    args = parser.parse_args()

    from mclaw.agents.enterprise_templates import ensure_enterprise_templates
    from mclaw.agents.presets import _DEPARTMENT_KB
    from mclaw.config import settings
    from mclaw.memory.knowledge_manager import KnowledgeManager

    project_root = getattr(settings, "project_root", None) or Path.cwd()
    data_dir = args.data_dir or (Path(project_root) / "data" / "knowledge")

    km = KnowledgeManager(data_dir=data_dir)
    try:
        for meta in _DEPARTMENT_KB.values():
            km.ensure_collection(
                meta.collection_id,
                meta.name,
                meta.description,
                workspace_id="default",
                owner_id="",
                is_public=False,
            )
        processed = ensure_enterprise_templates(km, force=args.force)
    finally:
        # KnowledgeManager 的后台初始化线程是 daemon，无需显式关闭；
        # 这里只确保 SQLite 连接随进程退出释放。
        pass

    if processed:
        print(f"已处理 {processed} 份模板 -> {data_dir}")
        return 0
    print(f"没有模板被导入，请检查部门库或模板文件: {data_dir}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
