"""知识库端点：endpoint_manager 存储 + KnowledgeManager 优先读取"""

from __future__ import annotations

import json

from mclaw.llm.endpoint_manager import EndpointManager
from mclaw.memory import knowledge_manager as km_module


def test_endpoint_manager_accepts_knowledge_endpoints(tmp_path):
    config_path = tmp_path / "data" / "llm_endpoints.json"
    mgr = EndpointManager(tmp_path, config_path=config_path)

    saved = mgr.save_endpoint(
        {
            "name": "kb-rerank",
            "provider": "dashscope",
            "api_type": "rerank",
            "base_url": "https://example.com/rerank",
            "model": "qwen3.7-text-rerank",
            "priority": 10,
            "extra_params": {"style": "dashscope", "timeout": 5},
        },
        api_key="sk-test",
        endpoint_type="knowledge_endpoints",
    )

    assert saved["api_key_env"]
    endpoints = mgr.list_endpoints("knowledge_endpoints")
    assert [ep["name"] for ep in endpoints] == ["kb-rerank"]
    assert "sk-test" in (tmp_path / ".env").read_text(encoding="utf-8")


def _write_knowledge_config(root, endpoints):
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "llm_endpoints.json").write_text(
        json.dumps({"knowledge_endpoints": endpoints}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_km_prefers_enabled_knowledge_endpoint(tmp_path, monkeypatch):
    root = tmp_path
    (root / ".env").write_text("UI_KB_RERANK_KEY=sk-ui\n", encoding="utf-8")
    _write_knowledge_config(
        root,
        [
            {
                "name": "ui-kb",
                "provider": "dashscope",
                "api_type": "rerank",
                "base_url": "https://example.com/native-rerank",
                "model": "qwen3.7-text-rerank",
                "api_key_env": "UI_KB_RERANK_KEY",
                "priority": 10,
                "enabled": True,
                "extra_params": {"style": "dashscope", "timeout": 5},
            }
        ],
    )
    monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
    monkeypatch.setattr(km_module, "_settings_opt_bool", lambda key, fallback=None: fallback)

    km = km_module.KnowledgeManager(
        data_dir=root / "data" / "knowledge",
        vector_backend="off",
    )

    assert km.reranker_enabled is True
    assert km._reranker_api_base == "https://example.com/native-rerank"
    assert km._reranker_api_model == "qwen3.7-text-rerank"
    assert km._reranker_api_key == "sk-ui"
    assert km._reranker_api_style == "dashscope"
    assert km._reranker_timeout_s == 5.0
    assert km._knowledge_endpoint_name == "ui-kb"


def test_km_falls_back_when_endpoint_disabled(tmp_path, monkeypatch):
    root = tmp_path
    _write_knowledge_config(
        root,
        [
            {
                "name": "disabled-kb",
                "base_url": "https://example.com/should-not-use",
                "model": "qwen3.7-text-rerank",
                "enabled": False,
            }
        ],
    )
    monkeypatch.setattr(km_module, "_lazy_import_chroma", lambda: False)
    monkeypatch.setattr(km_module, "_settings_opt_bool", lambda key, fallback=None: fallback)

    km = km_module.KnowledgeManager(
        data_dir=root / "data" / "knowledge",
        vector_backend="off",
        reranker_api_base="https://env.example.com/rerank",
        reranker_api_key="env-key",
        reranker_api_model="env-model",
    )

    assert km._knowledge_endpoint_name == ""
    assert km._reranker_api_base == "https://env.example.com/rerank"
    assert km._reranker_api_model == "env-model"
