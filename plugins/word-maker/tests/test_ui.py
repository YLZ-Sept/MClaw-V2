from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ui_contains_avatar_studio_aligned_tabs() -> None:
    html = (ROOT / "ui" / "dist" / "index.html").read_text(encoding="utf-8")

    for tab in ["create", "projects", "templates", "draft", "settings", "guide"]:
        assert tab in html
    assert "open-folder" in html
    assert "storage/list-dir" in html
    assert "deps/check" in html
    assert "/upload" in html
    assert "/render" in html
    assert "/outline/generate" in html
    assert "/exports/" in html
    assert "oaConfirm" in html
    assert "ak-logo" in html
    assert "oa-config-banner" in html
    assert "split-left" in html
    assert "split-right" in html
    assert "oa-preview-area" in html
    assert "mode-card" in html
    assert "localStorage" in html
    assert "模板变量检测" in html


def test_ui_uses_self_contained_assets() -> None:
    html = (ROOT / "ui" / "dist" / "index.html").read_text(encoding="utf-8")

    assert "_assets/bootstrap.js" in html
    assert "/api/plugins/_sdk" not in html


def test_ui_bootstrap_uses_current_bridge_protocol_and_api_proxy() -> None:
    bootstrap = (ROOT / "ui" / "dist" / "_assets" / "bootstrap.js").read_text(
        encoding="utf-8"
    )

    assert 'send("bridge:ready")' in bootstrap
    assert 'message.type === "bridge:init"' in bootstrap
    assert 'bridgeRequest("bridge:api-request"' in bootstrap
    assert 'message.type === "bridge:api-response"' in bootstrap
    assert '"/api/plugins/" + encodeURIComponent(meta.pluginId)' in bootstrap
    assert "bridge:config" not in bootstrap


def test_dependency_check_updates_status_and_reports_failures() -> None:
    html = (ROOT / "ui" / "dist" / "index.html").read_text(encoding="utf-8")

    assert "brain_status: r.brain_status || {}" in html
    assert "brain_available: Boolean(r.brain_status && r.brain_status.available)" in html
    assert "dependency_report: r.dependency_report || {}" in html
    assert 'toast("依赖检测完成")' in html
    assert 'toast("依赖检测失败："' in html
    assert 'state.depsChecking ? "检测中..."' in html
    assert 'window.addEventListener("mclaw:ready"' in html
