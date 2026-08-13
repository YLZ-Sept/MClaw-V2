"""L1 Unit Tests: 知识库 API 权限 —— 公共资源模型。

知识库应作为公共资源池：任何人（非 owner）都能上传、删除、读 collection。
Agent 调用时仍按绑定集合隔离（见 test_knowledge_manager.py / tools/handlers/knowledge.py），
本文件只覆盖 HTTP API 层的「读写对所有人开放」。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mclaw.api.routes import knowledge as kr
from mclaw.memory import knowledge_manager as km_module
from mclaw.memory.knowledge_manager import KnowledgeManager


def _make_client(monkeypatch, tmp_path) -> tuple[TestClient, KnowledgeManager]:
    monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
    km = KnowledgeManager(data_dir=tmp_path)
    monkeypatch.setattr(kr, "_knowledge_manager", km)
    app = FastAPI()
    app.include_router(kr.router)
    return TestClient(app), km


def _create_collection(client: TestClient, name: str, user: str) -> str:
    resp = client.post(
        "/api/knowledge/collections",
        json={"name": name, "description": "d", "is_public": False},
        headers={"X-Mclaw-User": user},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestKnowledgeApiAccess:
    def test_non_owner_can_read_private_collection(self, tmp_path, monkeypatch):
        client, _ = _make_client(monkeypatch, tmp_path)
        coll_id = _create_collection(client, "私有集合", "owner-A")

        resp = client.get(
            f"/api/knowledge/collections/{coll_id}",
            headers={"X-Mclaw-User": "other-user"},
        )
        assert resp.status_code == 200

    def test_non_owner_can_delete_collection(self, tmp_path, monkeypatch):
        client, _ = _make_client(monkeypatch, tmp_path)
        coll_id = _create_collection(client, "集合", "owner-A")

        resp = client.delete(
            f"/api/knowledge/collections/{coll_id}",
            headers={"X-Mclaw-User": "other-user"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_non_owner_can_upload_document(self, tmp_path, monkeypatch):
        client, _ = _make_client(monkeypatch, tmp_path)
        coll_id = _create_collection(client, "集合", "owner-A")

        resp = client.post(
            f"/api/knowledge/collections/{coll_id}/upload",
            files={"file": ("test.txt", b"hello world", "text/plain")},
            headers={"X-Mclaw-User": "other-user"},
        )
        assert resp.status_code == 201
