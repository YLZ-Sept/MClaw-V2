"""技能级权限判断（执行期）。

``AgentFactory._apply_skill_filter`` 的 INCLUSIVE 模式只把未选技能从系统
提示的目录里隐藏，``list_skills`` / ``get_skill_info`` /
``get_skill_reference`` / ``run_skill_script`` / ``execute_skill`` 仍能使用它们。
本模块提供与工具白名单等价的语义，供技能 handler 在执行前强校验。

只依赖标准库；对 profile/store 的导入保持惰性，避免 handler → agents →
agent runtime 的导入环。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ESSENTIAL_SYSTEM_SKILLS: frozenset[str] = frozenset(
    {
        # 规划（多步任务的核心）
        "create-todo",
        "update-todo-step",
        "get-todo-status",
        "complete-todo",
        # 技能发现（渐进式披露入口 — 外部技能必须先 get_skill_info 读指令）
        "get-skill-info",
        "list-skills",
        # 文件系统（外部技能执行的基础 — 读指令→写代码→run-shell 执行）
        "run-shell",
        "read-file",
        "write-file",
        "list-directory",
        # IM 通道（接收用户输入、交付文件）
        "deliver-artifacts",
        "get-chat-history",
        "get-image-file",
        "get-voice-file",
        # 记忆
        "search-memory",
        "add-memory",
        # 信息检索
        "web-search",
        # 系统
        "get-tool-info",
        "set-task-timeout",
    }
)


def normalize_skill_name(name: str) -> str:
    """归一化技能名：下划线转连字符、统一小写。"""
    return (name or "").strip().lower().replace("_", "-")


def build_skill_match_set(names: Iterable[str]) -> tuple[set[str], set[str]]:
    """构建技能名匹配集，同时支持完整命名空间和 ``@`` 短名匹配。"""
    exact: set[str] = set()
    short: set[str] = set()
    for name in names:
        norm = normalize_skill_name(name)
        if not norm:
            continue
        exact.add(norm)
        short.add(norm.split("@", 1)[-1] if "@" in norm else norm)
    return exact, short


def skill_in_set(skill_name: str, exact_set: set[str], short_set: set[str]) -> bool:
    """判断技能名是否在匹配集中（兼容命名空间和短名）。"""
    norm = normalize_skill_name(skill_name)
    if norm in exact_set:
        return True
    return (norm.split("@", 1)[-1] if "@" in norm else norm) in short_set


def is_essential_skill(skill_name: str) -> bool:
    """判断是否为基础设施系统工具（INCLUSIVE/EXCLUSIVE 始终保留）。"""
    return normalize_skill_name(skill_name) in ESSENTIAL_SYSTEM_SKILLS


def _skills_mode_value(profile: Any) -> str:
    mode = getattr(profile, "skills_mode", "all")
    return getattr(mode, "value", mode)


def is_skill_allowed(profile: Any | None, skill_name: str) -> bool:
    """按 profile 的技能模式判断某个技能是否可用。

    - 无 profile / 未知模式：fail-open（与工具守卫一致）
    - ``all``：全部允许
    - essential 系统技能：始终允许
    - ``inclusive``：仅允许 profile.skills 中的技能
    - ``exclusive``：拒绝 profile.skills 中的技能
    """
    if profile is None:
        return True
    mode = _skills_mode_value(profile)
    if mode == "all":
        return True
    if is_essential_skill(skill_name):
        return True
    exact, short = build_skill_match_set(getattr(profile, "skills", []) or [])
    if mode == "inclusive":
        return skill_in_set(skill_name, exact, short)
    if mode == "exclusive":
        return not skill_in_set(skill_name, exact, short)
    return True


def filter_skill_entries(entries: Iterable[Any], profile: Any | None) -> list[Any]:
    """过滤技能条目；条目没有可识别名称时保留。"""
    allowed: list[Any] = []
    for entry in entries:
        name = getattr(entry, "skill_id", None) or getattr(entry, "name", None)
        if not name or is_skill_allowed(profile, name):
            allowed.append(entry)
    return allowed


def current_profile_for_agent(agent: Any) -> Any | None:
    """取当前 Agent 绑定的 profile；组织节点通过 context 里的 profile_id 回退。"""
    profile = getattr(agent, "_agent_profile", None) if agent is not None else None
    if profile is not None:
        return profile
    from .agent_context import get_current_agent_profile_id

    profile_id = get_current_agent_profile_id()
    if not profile_id:
        return None
    from ..agents.profile import get_profile_store

    return get_profile_store().get(profile_id)
