"""企业运营内置模板：入仓完整性、部门库绑定与幂等导入。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mclaw.agents.enterprise_templates import (
    TEMPLATE_ROOT,
    TEMPLATES,
    ensure_enterprise_templates,
)
from mclaw.agents.presets import _DEPARTMENT_KB, _DIGITAL_EMPLOYEE_KB_BINDINGS


class _FakeKM:
    def __init__(
        self,
        data_dir: Path,
        collections: set[str] | None = None,
        existing_checksums: set[str] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self._collections = set(collections or ())
        self._existing_checksums = set(existing_checksums or ())
        self.ingested: list[tuple[str, Path, str]] = []

    def get_collection(self, collection_id: str):
        return object() if collection_id in self._collections else None

    def ingest_file(self, collection_id: str, file_path: Path, uploaded_by: str = ""):
        self.ingested.append((collection_id, Path(file_path), uploaded_by))
        return object()

    def list_documents(self, collection_id: str, offset: int = 0, limit: int = 50):
        docs = [
            type("Doc", (), {"checksum": checksum})()
            for checksum in self._existing_checksums
        ]
        return docs, len(docs)

    def delete_document(self, doc_id: str) -> int:
        return 1


def test_all_template_files_are_present():
    assert len(TEMPLATES) == 10
    for template in TEMPLATES:
        assert template.source_path.is_file(), template.source_path
        assert template.source_path.parent == TEMPLATE_ROOT / template.role_id


def test_template_bindings_match_department_kb():
    collection_ids = {meta.collection_id for meta in _DEPARTMENT_KB.values()}
    for template in TEMPLATES:
        assert template.collection_id in collection_ids, template
        assert template.collection_id in _DIGITAL_EMPLOYEE_KB_BINDINGS[template.role_id], template


def test_ensure_uses_stable_seed_path(tmp_path):
    fake = _FakeKM(tmp_path, {t.collection_id for t in TEMPLATES})

    processed = ensure_enterprise_templates(fake)

    assert processed == 10
    assert len(fake.ingested) == 10
    collection_ids = {meta.collection_id for meta in _DEPARTMENT_KB.values()}
    for collection_id, path, uploaded_by in fake.ingested:
        assert collection_id in collection_ids
        assert path.is_file()
        assert path.is_relative_to(tmp_path / "enterprise_templates")
        assert uploaded_by == "system:enterprise-templates"


def test_ensure_skips_missing_collection(tmp_path):
    fake = _FakeKM(tmp_path, set())

    processed = ensure_enterprise_templates(fake)

    assert processed == 0
    assert fake.ingested == []


def test_ensure_skips_content_already_present(tmp_path):
    from mclaw.agents.enterprise_templates import _sha256

    checksums = {_sha256(template.source_path) for template in TEMPLATES}
    fake = _FakeKM(
        tmp_path,
        {template.collection_id for template in TEMPLATES},
        existing_checksums=checksums,
    )

    processed = ensure_enterprise_templates(fake)

    # 旧版本上传的模板文件名带时间戳，但内容相同；不应再入库一份。
    assert processed == 10
    assert fake.ingested == []


def test_import_is_idempotent(tmp_path):
    from mclaw.memory.knowledge_manager import KnowledgeManager

    km = KnowledgeManager(
        data_dir=tmp_path / "kb",
        vector_backend="off",
        reranker_enabled=False,
    )
    for meta in _DEPARTMENT_KB.values():
        km.ensure_collection(
            meta.collection_id,
            meta.name,
            meta.description,
            workspace_id="default",
            owner_id="",
            is_public=False,
        )

    assert ensure_enterprise_templates(km) == 10
    counts_first = {
        meta.collection_id: km.list_documents(meta.collection_id, limit=50)[1]
        for meta in _DEPARTMENT_KB.values()
    }
    assert sum(counts_first.values()) == 10

    # 第二次导入时文件未变化，ingest_file 按 checksum 跳过，不应产生重复文档。
    assert ensure_enterprise_templates(km) == 10
    counts_second = {
        meta.collection_id: km.list_documents(meta.collection_id, limit=50)[1]
        for meta in _DEPARTMENT_KB.values()
    }
    assert counts_second == counts_first
