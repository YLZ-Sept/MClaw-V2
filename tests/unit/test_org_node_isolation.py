"""Organisation nodes must use their role's KB and isolated memory."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mclaw.core.agent_context import (
    get_current_agent_profile_id,
    get_current_memory_manager,
    reset_current_agent_context,
    set_current_agent_context,
)
from mclaw.orgs._default_agent_builder import _BrainBackedNodeAgent
from mclaw.orgs._runtime_agent_pipeline import AgentSpec, ProfileResolver
from mclaw.tools.handlers.knowledge import KnowledgeHandler
from mclaw.tools.handlers.memory import MemoryHandler


def test_memory_handler_prefers_node_scoped_manager():
    main_manager = object()
    node_manager = object()
    handler = MemoryHandler(SimpleNamespace(memory_manager=main_manager))

    assert handler._memory_manager is main_manager

    tokens = set_current_agent_context(
        profile_id="finance-management", memory_manager=node_manager
    )
    try:
        assert handler._memory_manager is node_manager
    finally:
        reset_current_agent_context(tokens)

    assert handler._memory_manager is main_manager


async def test_node_agent_sets_and_resets_context(monkeypatch):
    spec = AgentSpec(
        org_id="org",
        node_id="finance-node",
        role="worker",
        agent_profile_id="finance-management",
    )
    node = _BrainBackedNodeAgent(spec, brain=object())
    sentinel = object()
    node._memory_manager = sentinel  # type: ignore[attr-defined]

    seen: dict[str, object] = {}

    async def fake_inner(self, content, *, cancel_event=None):
        seen["profile"] = get_current_agent_profile_id()
        seen["memory_manager"] = get_current_memory_manager()
        return f"ok:{content}"

    monkeypatch.setattr(_BrainBackedNodeAgent, "_run_inner", fake_inner)

    assert await node.run("hello") == "ok:hello"
    assert seen == {
        "profile": "finance-management",
        "memory_manager": sentinel,
    }
    assert get_current_agent_profile_id() is None
    assert get_current_memory_manager() is None


async def test_knowledge_handler_uses_node_profile_collections(monkeypatch):
    calls: list[str] = []

    class _FakeKM:
        def search_with_chunks(self, query, collection_id, top_k):
            calls.append(collection_id)
            return []

    class _FakeStore:
        def get(self, profile_id):
            if profile_id == "finance-management":
                return SimpleNamespace(knowledge_collections=["sys-dept-finance"])
            return SimpleNamespace(knowledge_collections=["sys-dept-main"])

    monkeypatch.setattr("mclaw.agents.profile.get_profile_store", lambda: _FakeStore())
    monkeypatch.setattr(
        "mclaw.api.routes.knowledge.get_knowledge_manager", lambda: _FakeKM()
    )

    handler = KnowledgeHandler(SimpleNamespace(_agent_profile_id="main-profile"))
    tokens = set_current_agent_context(profile_id="finance-management")
    try:
        result = await handler._knowledge_search({"query": "工资复核", "top_k": 2})
    finally:
        reset_current_agent_context(tokens)

    assert "未找到" in result
    assert calls == ["sys-dept-finance"]


def test_profile_resolver_carries_node_agent_profile_id():
    node = SimpleNamespace(
        id="finance-node",
        role_title="财务管理",
        role="worker",
        agent_profile_id="finance-management",
        external_tools=["read_file"],
        can_delegate=False,
        custom_prompt="你是财务管理岗位。",
    )
    org = SimpleNamespace(nodes={"finance-node": node}, edges=[], workspace_dir=None)

    class _Lookup:
        def get_org(self, org_id):
            return org if org_id == "org" else None

    spec = ProfileResolver(lookup=_Lookup()).resolve(org_id="org", node_id="finance-node")

    assert spec is not None
    assert spec.agent_profile_id == "finance-management"


def test_leaf_node_does_not_treat_escalation_edge_as_delegation():
    finance = SimpleNamespace(
        id="finance-management",
        role_title="财务管理",
        can_delegate=False,
    )
    ops = SimpleNamespace(
        id="business-operations",
        role_title="经营管理",
        can_delegate=True,
    )
    org = SimpleNamespace(
        nodes={"finance-management": finance, "business-operations": ops},
        edges=[
            SimpleNamespace(
                source="business-operations",
                target="finance-management",
                edge_type="hierarchy",
            ),
            SimpleNamespace(
                source="finance-management",
                target="business-operations",
                edge_type="escalate",
            ),
        ],
    )

    assert ProfileResolver._available_nodes_for(org, "finance-management") == ()
    assert ProfileResolver._available_nodes_for(org, "business-operations") == (
        ("finance-management", "财务管理"),
    )
