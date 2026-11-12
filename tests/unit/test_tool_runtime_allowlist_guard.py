"""执行时白名单守卫：防止模型发明已过滤的工具名绕过 profile 权限。"""

from __future__ import annotations

from types import SimpleNamespace

from mclaw.core._tool_runtime import ToolExecutor


def _executor(tools: list[str], profile_id: str = "customer-management") -> ToolExecutor:
    executor = ToolExecutor(handler_registry=SimpleNamespace())
    executor._agent_ref = SimpleNamespace(
        _tools=[{"name": name} for name in tools],
        _agent_profile_id=profile_id,
    )
    return executor


def test_blocks_tool_not_in_agent_allowlist():
    executor = _executor(["read_file", "write_file", "deliver_artifacts"])

    assert executor._blocked_tool_reason("run_shell") is not None
    assert executor._blocked_tool_reason("delete_file") is not None
    assert executor._blocked_tool_reason("read_file") is None


def test_allows_control_tools_even_if_absent_from_allowlist():
    executor = _executor(["read_file"])

    for name in (
        "ask_user",
        "get_tool_info",
        "set_task_timeout",
        "task_stop",
        "tool_search",
        "exit_plan_mode",
    ):
        assert executor._blocked_tool_reason(name) is None


def test_empty_tool_list_does_not_block():
    executor = _executor([])
    assert executor._blocked_tool_reason("run_shell") is None


def test_missing_agent_does_not_block():
    executor = ToolExecutor(handler_registry=SimpleNamespace())
    assert executor._blocked_tool_reason("run_shell") is None


def test_enterprise_ops_allowlist_includes_ask_user():
    from mclaw.agents.presets import get_preset_by_id

    preset = get_preset_by_id("customer-management")
    assert "ask_user" in preset.tools
    assert "run_shell" not in preset.tools
