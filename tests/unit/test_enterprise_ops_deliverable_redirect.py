"""企业运营岗位交付物落盘重定向。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mclaw.tools.handlers.filesystem import FilesystemHandler


def _handler(profile_id: str, working_dir: Path, workspace: Path, category: str = "enterprise-ops"):
    agent = SimpleNamespace(_agent_profile_id=profile_id)
    handler = FilesystemHandler(agent)
    handler._working_directory = lambda: working_dir  # type: ignore[assignment]
    store = SimpleNamespace(get=lambda _pid: SimpleNamespace(category=category))
    return handler, store


def test_relative_deliverable_redirects_to_working_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    working = workspace / "deliverables" / "acceptance" / "project-management"
    working.mkdir(parents=True)
    handler, store = _handler("project-management", working, workspace)
    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: store)
    monkeypatch.setattr("mclaw.core.working_directory.config_workspace", lambda: workspace)

    redirected = handler._redirect_enterprise_ops_deliverable("项目周报.md")

    assert Path(redirected) == working / "项目周报.md"


def test_absolute_workspace_deliverable_redirects_to_working_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    working = workspace / "deliverables" / "acceptance" / "project-management"
    working.mkdir(parents=True)
    handler, store = _handler("project-management", working, workspace)
    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: store)
    monkeypatch.setattr("mclaw.core.working_directory.config_workspace", lambda: workspace)

    redirected = handler._redirect_enterprise_ops_deliverable(
        str(workspace / "data" / "temp" / "项目周报.md")
    )

    assert Path(redirected) == working / "项目周报.md"


def test_temp_script_and_non_enterprise_paths_are_not_redirected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    working = workspace / "deliverables" / "acceptance" / "project-management"
    working.mkdir(parents=True)
    handler, store = _handler("project-management", working, workspace)
    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: store)
    monkeypatch.setattr("mclaw.core.working_directory.config_workspace", lambda: workspace)

    script = str(workspace / "data" / "temp" / "gen.py")
    assert handler._redirect_enterprise_ops_deliverable(script) == script

    outside = str(tmp_path / "somewhere" / "report.md")
    assert handler._redirect_enterprise_ops_deliverable(outside) == outside


def test_non_enterprise_profile_is_not_redirected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    working = workspace / "deliverables" / "acceptance" / "project-management"
    working.mkdir(parents=True)
    handler, store = _handler("some-custom-agent", working, workspace, category="general")
    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: store)
    monkeypatch.setattr("mclaw.core.working_directory.config_workspace", lambda: workspace)

    assert handler._redirect_enterprise_ops_deliverable("报告.md") == "报告.md"
