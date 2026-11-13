"""企业运营 8 岗位的工具与技能白名单。

预置（agents/presets.py）和组织模板（orgs/_runtime_templates.py）共用同
一张表，避免两处手工维护后漂移。本模块只依赖标准库，不能导入
``mclaw.agents`` / ``mclaw.agent`` / ``mclaw.orgs``：组织模板的 bootstrap
不应被 agent runtime 的导入链路拖进来。
"""

from __future__ import annotations

from collections.abc import Iterable

ENTERPRISE_OPS_ROLE_IDS: tuple[str, ...] = (
    "business-operations",
    "sales-management",
    "commerce-management",
    "finance-management",
    "hr-management",
    "project-management",
    "customer-management",
    "general-management",
)

# 所有岗位都要的文档/表格/知识库/交付能力。不含 shell、删除、浏览器、
# 桌面、定时、研究、MCP、插件；也不含跨岗位可读的会话全文检索
# （search_conversation_traces）和可写用户画像的 update_user_profile。
ENTERPRISE_OPS_BASE_TOOLS: tuple[str, ...] = (
    # 交互与计划
    "ask_user",
    "create_todo",
    "update_todo_step",
    "get_todo_status",
    "complete_todo",
    "create_plan_file",
    # 文件读写（不含 shell / delete_file）
    "read_file",
    "write_file",
    "edit_file",
    "append_file",
    "list_directory",
    "glob",
    "grep",
    "move_file",
    # 交付与附件
    "deliver_artifacts",
    "get_chat_history",
    "get_image_file",
    # 知识库检索
    "knowledge_search",
    # 技能
    "run_skill_script",
    "list_skills",
    "get_skill_info",
    "get_skill_reference",
    # 记忆
    "add_memory",
    "search_memory",
)

ENTERPRISE_OPS_ROLE_EXTRA_TOOLS: dict[str, tuple[str, ...]] = {
    # 经营分析要复盘跨岗位历史任务，客户侧会有语音留言。
    "business-operations": ("get_voice_file", "get_memory_stats", "list_recent_tasks"),
    # 销售线索、客户沟通可能以语音形式进入。
    "sales-management": ("get_voice_file",),
    "customer-management": ("get_voice_file",),
}

ENTERPRISE_OPS_FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "run_shell",
        "run_powershell",
        "delete_file",
        "semantic_search",
        "search_conversation_traces",
        "get_user_profile",
        "update_user_profile",
        "skip_profile_question",
        "toggle_proactive",
        "browser",
        "desktop",
        "scheduled",
        "research",
        "mcp",
        "plugins",
    }
)

ENTERPRISE_OPS_BASE_SKILLS: tuple[str, ...] = (
    "docx",
    "xlsx",
    "pptx",
    "pdf",
    "doc-coauthoring",
    "baidu-paddleocr-doc",
)

ENTERPRISE_OPS_ROLE_EXTRA_SKILLS: dict[str, tuple[str, ...]] = {
    "business-operations": ("pretty-mermaid", "internal-comms"),
    "sales-management": ("bid-writer",),
    "commerce-management": ("bid-writer",),
    "finance-management": ("pretty-mermaid",),
    "hr-management": ("chinese-writing", "internal-comms"),
    "project-management": ("pretty-mermaid",),
    "customer-management": ("chinese-writing",),
    "general-management": ("chinese-writing", "internal-comms", "datetime-tool"),
}


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def enterprise_ops_tools(role_id: str) -> list[str]:
    """返回岗位的完整工具白名单（基础 + 岗位追加，去重保序）。"""
    if role_id not in ENTERPRISE_OPS_ROLE_IDS:
        raise KeyError(f"Unknown enterprise-ops role: {role_id}")
    return _dedupe(
        (*ENTERPRISE_OPS_BASE_TOOLS, *ENTERPRISE_OPS_ROLE_EXTRA_TOOLS.get(role_id, ()))
    )


def enterprise_ops_skills(role_id: str) -> list[str]:
    """返回岗位的完整技能白名单（基础 + 岗位追加，去重保序）。"""
    if role_id not in ENTERPRISE_OPS_ROLE_IDS:
        raise KeyError(f"Unknown enterprise-ops role: {role_id}")
    return _dedupe(
        (*ENTERPRISE_OPS_BASE_SKILLS, *ENTERPRISE_OPS_ROLE_EXTRA_SKILLS.get(role_id, ()))
    )


def validate_enterprise_ops_permissions(
    role_id: str, tools: Iterable[str], skills: Iterable[str]
) -> None:
    """校验岗位权限表不包含禁用项；供测试和部署自检调用。"""
    tool_set = set(tools)
    forbidden = sorted(ENTERPRISE_OPS_FORBIDDEN_TOOLS & tool_set)
    if forbidden:
        raise AssertionError(f"{role_id} 包含禁用工具: {forbidden}")
    if not skills:
        raise AssertionError(f"{role_id} 没有配置技能白名单")
