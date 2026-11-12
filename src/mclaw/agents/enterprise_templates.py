"""企业运营岗位内置模板：随包分发、幂等导入部门知识库。

模板源文件放在 ``src/mclaw/builtin_templates/enterprise_ops/<role>/``，
随 wheel 一起分发。首次启动或手动脚本会把文件复制到
``<knowledge data_dir>/enterprise_templates/<role>/``，再调用
``KnowledgeManager.ingest_file``；后者按 (collection_id, file_path, checksum)
去重，重复启动不会产生重复文档，模板内容升级后会重新索引。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mclaw.memory.knowledge_manager import KnowledgeManager

logger = logging.getLogger(__name__)

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "builtin_templates" / "enterprise_ops"

# 系统导入的文档使用固定上传者标记，便于 --force 时只清理系统模板。
SYSTEM_UPLOADER = "system:enterprise-templates"


@dataclass(frozen=True)
class EnterpriseTemplate:
    role_id: str
    collection_id: str
    filename: str

    @property
    def source_path(self) -> Path:
        return TEMPLATE_ROOT / self.role_id / self.filename


# 10 份模板，按岗位绑定到 5 个部门库。顺序保持稳定，便于日志核对。
TEMPLATES: tuple[EnterpriseTemplate, ...] = (
    EnterpriseTemplate("finance-management", "sys-dept-finance", "工资复核清单.xlsx"),
    EnterpriseTemplate("finance-management", "sys-dept-finance", "应收账款账龄表.xlsx"),
    EnterpriseTemplate("sales-management", "sys-dept-sales", "销售周报模板.docx"),
    EnterpriseTemplate("sales-management", "sys-dept-sales", "客户线索分级表.xlsx"),
    EnterpriseTemplate("hr-management", "sys-dept-hr", "招聘跟进表.xlsx"),
    EnterpriseTemplate("hr-management", "sys-dept-hr", "考勤异常清单.xlsx"),
    EnterpriseTemplate("project-management", "sys-dept-project", "项目周报模板.docx"),
    EnterpriseTemplate("project-management", "sys-dept-project", "里程碑与风险台账.xlsx"),
    EnterpriseTemplate("general-management", "sys-dept-general", "制度修订对照表.docx"),
    EnterpriseTemplate("general-management", "sys-dept-general", "社保办理清单.xlsx"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_path(km: KnowledgeManager, template: EnterpriseTemplate) -> Path:
    return Path(km.data_dir) / "enterprise_templates" / template.role_id / template.filename


def _sync_source(km: KnowledgeManager, template: EnterpriseTemplate) -> Path:
    """把包内模板复制到知识库数据目录，返回稳定的入库路径。

    使用稳定的数据目录路径而不是 site-packages 路径，避免升级/换安装位置后
    ``ingest_file`` 把同一份模板当成新文档。内容未变时不复制。
    """
    source = template.source_path
    if not source.is_file():
        raise FileNotFoundError(f"内置模板缺失: {source}")

    target = _seed_path(km, template)
    if target.is_file() and _sha256(target) == _sha256(source):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _delete_system_doc(km: KnowledgeManager, template: EnterpriseTemplate) -> None:
    items, _ = km.list_documents(template.collection_id, offset=0, limit=200)
    for doc in items:
        if doc.filename == template.filename and doc.uploaded_by == SYSTEM_UPLOADER:
            km.delete_document(doc.id)


def ensure_enterprise_templates(
    km: KnowledgeManager | None = None,
    *,
    force: bool = False,
) -> int:
    """确保 10 份内置模板存在于对应部门知识库。

    - 部门库不存在时跳过该文件（骨架通常已由 presets 部署时创建）
    - 单个文件失败只记日志，不阻断其他文件
    - ``force=True`` 时先删除同一文件名且由系统导入的旧文档，再重新入库

    Returns:
        成功提交给 ``ingest_file`` 的文件数；未变化的文件由底层按 checksum 跳过。
    """
    if km is None:
        from mclaw.api.routes.knowledge import get_knowledge_manager

        km = get_knowledge_manager()

    processed = 0
    for template in TEMPLATES:
        if km.get_collection(template.collection_id) is None:
            logger.warning(
                "[EnterpriseTemplates] 部门库不存在，跳过 %s: %s",
                template.filename,
                template.collection_id,
            )
            continue
        try:
            target = _sync_source(km, template)
            source_checksum = _sha256(target)
            if not force:
                # 旧版本手工上传的模板文件名带时间戳前缀、上传者不是
                # SYSTEM_UPLOADER；按 checksum 识别“内容已经在库里”，
                # 避免升级后同一份模板出现两篇。
                existing_docs, _ = km.list_documents(
                    template.collection_id, offset=0, limit=500
                )
                if any(doc.checksum == source_checksum for doc in existing_docs):
                    logger.debug(
                        "[EnterpriseTemplates] 内容已存在，跳过 %s (%s)",
                        template.filename,
                        template.collection_id,
                    )
                    processed += 1
                    continue
            if force:
                _delete_system_doc(km, template)
            km.ingest_file(template.collection_id, target, uploaded_by=SYSTEM_UPLOADER)
            processed += 1
        except Exception as exc:  # noqa: BLE001 — 单文件失败不阻断其余模板
            logger.warning("[EnterpriseTemplates] 导入失败 %s: %s", template.filename, exc)

    if processed:
        logger.info("[EnterpriseTemplates] 已确保 %d 份部门模板可检索", processed)
    return processed
