"""企业运营 8 岗组织模板：节点、Profile 绑定、工具边界与落盘。"""

from __future__ import annotations

import json

from mclaw.orgs._runtime_templates import (
    ALL_TEMPLATES,
    ENTERPRISE_OPERATIONS,
    TEMPLATE_POLICY_MAP,
    _with_builtin_metadata,
    ensure_builtin_templates,
)

ROOT_ID = "business-operations"
ROLE_IDS = {
    "sales-management",
    "commerce-management",
    "finance-management",
    "hr-management",
    "project-management",
    "customer-management",
    "general-management",
}


def test_template_registered():
    assert ALL_TEMPLATES["enterprise-operations"] is ENTERPRISE_OPERATIONS
    assert TEMPLATE_POLICY_MAP["enterprise-operations"] == "default"


def test_eight_nodes_and_profile_bindings():
    nodes = {node["id"]: node for node in ENTERPRISE_OPERATIONS["nodes"]}
    assert set(nodes) == {ROOT_ID} | ROLE_IDS

    root = nodes[ROOT_ID]
    assert root["level"] == 0
    assert root["can_delegate"] is True
    assert root["agent_profile_id"] == ROOT_ID

    for role_id in ROLE_IDS:
        node = nodes[role_id]
        assert node["level"] == 1
        assert node["agent_profile_id"] == role_id
        assert node["can_delegate"] is False
        assert node["custom_prompt"].strip()


def test_node_tools_match_role_matrix():
    from mclaw.core.enterprise_ops_permissions import (
        ENTERPRISE_OPS_FORBIDDEN_TOOLS,
        enterprise_ops_tools,
    )

    for node in ENTERPRISE_OPERATIONS["nodes"]:
        assert node["external_tools"] == enterprise_ops_tools(node["id"]), node["id"]
        assert node["enable_file_tools"] is False
        assert not ENTERPRISE_OPS_FORBIDDEN_TOOLS & set(node["external_tools"]), node["id"]
        assert "knowledge_search" in node["external_tools"]
        assert "deliver_artifacts" in node["external_tools"]
        assert "ask_user" in node["external_tools"]


def test_hierarchy_and_cross_role_edges():
    edges = ENTERPRISE_OPERATIONS["edges"]
    hierarchy = [edge for edge in edges if edge["edge_type"] == "hierarchy"]
    assert len(hierarchy) == 7
    assert {edge["source"] for edge in hierarchy} == {ROOT_ID}
    assert {edge["target"] for edge in hierarchy} == ROLE_IDS
    assert any(edge["edge_type"] == "collaborate" for edge in edges)
    assert any(edge["edge_type"] == "escalate" for edge in edges)


def test_builtin_metadata_preserves_explicit_profile_ids():
    data = _with_builtin_metadata("enterprise-operations", ENTERPRISE_OPERATIONS)
    nodes = {node["id"]: node for node in data["nodes"]}
    for node_id in {ROOT_ID} | ROLE_IDS:
        assert nodes[node_id]["agent_profile_id"] == node_id
        assert nodes[node_id]["avatar"]


def test_resolved_node_tools_include_safe_subset():
    from mclaw.orgs._runtime_node_tools import resolve_node_tools

    node = next(
        node for node in ENTERPRISE_OPERATIONS["nodes"] if node["id"] == "finance-management"
    )
    tools = resolve_node_tools(
        external_tools=node["external_tools"],
        enable_file_tools=False,
        tool_host=None,
    )
    names = {tool["name"] for tool in tools}
    assert {"read_file", "write_file", "knowledge_search", "deliver_artifacts"} <= names
    assert "org_submit_deliverable" in names
    assert not {"run_shell", "delete_file", "browser_open"} & names


def test_ensure_builtin_templates_writes_enterprise_operations(tmp_path):
    ensure_builtin_templates(tmp_path)

    path = tmp_path / "enterprise-operations.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["nodes"]) == 8
    assert data["policy_template"] == "default"
    assert {node["agent_profile_id"] for node in data["nodes"]} == {ROOT_ID} | ROLE_IDS


def test_upgrade_enterprise_operations_tools_syncs_per_role_and_is_idempotent():
    from mclaw.core.enterprise_ops_permissions import enterprise_ops_tools
    from mclaw.orgs._runtime_templates import _upgrade_enterprise_operations_tools

    data = {
        "nodes": [
            {"id": "finance-management", "external_tools": ["read_file"]},
            {"id": "sales-management", "external_tools": ["ask_user", "read_file"]},
            {"id": "custom-node", "external_tools": ["read_file"]},
        ]
    }

    assert _upgrade_enterprise_operations_tools(data) is True
    nodes = {node["id"]: node for node in data["nodes"]}
    assert nodes["finance-management"]["external_tools"] == enterprise_ops_tools(
        "finance-management"
    )
    assert nodes["sales-management"]["external_tools"] == enterprise_ops_tools(
        "sales-management"
    )
    assert nodes["custom-node"]["external_tools"] == ["read_file"]
    assert nodes["finance-management"]["enable_file_tools"] is False
    assert _upgrade_enterprise_operations_tools(data) is False
