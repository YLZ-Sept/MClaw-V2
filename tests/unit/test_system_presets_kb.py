"""D 轮：SYSTEM 预置 ↔ 部门空知识库骨架的绑定一致性 + 部署回填。

覆盖：
1. 6 个数字员工预置的 knowledge_collections 与 _DIGITAL_EMPLOYEE_KB_BINDINGS 一致；
   骨架 id 互异且以 ``sys-`` 开头；除这 6 个外无其他 SYSTEM 预置声明非空绑定。
2. deploy_system_presets 的升级路径会把 knowledge_collections 回填到已部署的
   （非自定义、SYSTEM）profile 上。
3. KB manager 不可用（get_knowledge_manager 抛错）时 deploy 不抛异常、绑定照常写入。
"""

from __future__ import annotations

from mclaw.agents import presets as presets_mod
from mclaw.agents.presets import (
    SYSTEM_PRESETS,
    _DEPARTMENT_KB,
    _DIGITAL_EMPLOYEE_KB_BINDINGS,
    deploy_system_presets,
    get_preset_by_id,
)
from mclaw.agents.profile import AgentProfile, ProfileStore


def _preset_copy_without_kb(profile_id: str) -> AgentProfile:
    """返回一个与预置同内容、但 knowledge_collections 清空的副本（模拟 D 轮前部署）。"""
    data = get_preset_by_id(profile_id).to_dict()
    data["knowledge_collections"] = []
    return AgentProfile.from_dict(data)


class TestBindingsConsistency:
    def test_preset_bindings_match_mapping(self):
        for profile_id, expected in _DIGITAL_EMPLOYEE_KB_BINDINGS.items():
            preset = get_preset_by_id(profile_id)
            assert preset is not None, profile_id
            assert preset.knowledge_collections == expected, profile_id

    def test_skeleton_ids_distinct_and_reserved(self):
        ids = [meta.collection_id for meta in _DEPARTMENT_KB.values()]
        assert len(ids) == len(set(ids))
        assert len(ids) == 5
        assert all(cid.startswith("sys-") for cid in ids)

    def test_only_digital_employees_declare_kb_bindings(self):
        for preset in SYSTEM_PRESETS:
            if preset.id in _DIGITAL_EMPLOYEE_KB_BINDINGS:
                continue
            assert preset.knowledge_collections == [], (
                f"{preset.id} 不应在 SYSTEM 预置中声明知识库绑定"
            )


class TestDeployBackfillsKnowledgeBinding:
    def test_upgrade_path_writes_binding(self, tmp_path, monkeypatch):
        # 跳过真实 KB 骨架 ensure，聚焦 deploy 的回填逻辑
        monkeypatch.setattr(presets_mod, "_ensure_digital_employee_kb_skeletons", lambda: None)
        store = ProfileStore(tmp_path)
        store.save(_preset_copy_without_kb("finance-assistant"))

        deployed = deploy_system_presets(store)

        stored = store.get("finance-assistant")
        assert stored.knowledge_collections == ["sys-dept-finance"]
        # 绑定的目标骨架 id 已声明在 _DEPARTMENT_KB 中
        assert stored.knowledge_collections[0] in {
            meta.collection_id for meta in _DEPARTMENT_KB.values()
        }
        assert deployed >= 1

    def test_ops_binds_all_five(self, tmp_path, monkeypatch):
        monkeypatch.setattr(presets_mod, "_ensure_digital_employee_kb_skeletons", lambda: None)
        store = ProfileStore(tmp_path)
        store.save(_preset_copy_without_kb("ops-director-assistant"))

        deploy_system_presets(store)

        stored = store.get("ops-director-assistant")
        assert len(stored.knowledge_collections) == 5
        assert sorted(stored.knowledge_collections) == sorted(
            _DIGITAL_EMPLOYEE_KB_BINDINGS["ops-director-assistant"]
        )

    def test_kb_manager_unavailable_does_not_raise(self, tmp_path, monkeypatch):
        # KB manager 不可用：ensure 的懒加载 get_knowledge_manager 抛错，
        # helper 应静默吞掉，deploy 继续并把绑定写入。
        import mclaw.api.routes.knowledge as kr

        def _raise(*args, **kwargs):
            raise RuntimeError("KB manager 未就绪 (503)")

        monkeypatch.setattr(kr, "get_knowledge_manager", _raise)
        store = ProfileStore(tmp_path)
        store.save(_preset_copy_without_kb("tech-dept-assistant"))

        deploy_system_presets(store)  # 不应抛异常

        assert store.get("tech-dept-assistant").knowledge_collections == ["sys-dept-tech"]
