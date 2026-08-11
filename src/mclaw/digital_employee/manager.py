"""
数字员工管理器 — JSON 文件存储 + 智能路由
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .models import DigitalEmployee, EmployeeAgent

logger = logging.getLogger(__name__)


class DigitalEmployeeManager:
    """管理数字员工的 CRUD 和路由"""

    def __init__(self, data_dir: Path | None = None) -> None:
        if data_dir is None:
            from mclaw.config import settings
            data_dir = settings.project_root / "data" / "digital_employees"
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._data_dir / "_index.json"
        self._employees: dict[str, DigitalEmployee] = {}
        self._load_all()

    # ── 持久化 ────────────────────────────────────────────────────────

    def _employee_path(self, emp_id: str) -> Path:
        return self._data_dir / f"{emp_id}.json"

    def _save_one(self, emp: DigitalEmployee) -> None:
        emp.updated_at = time.time()
        self._employee_path(emp.id).write_text(
            json.dumps(emp.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_all(self) -> None:
        self._employees.clear()
        for f in sorted(self._data_dir.glob("*.json")):
            if f.name.startswith("_"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                emp = DigitalEmployee.from_dict(data)
                self._employees[emp.id] = emp
            except Exception as e:
                logger.warning(f"[DigitalEmployee] 加载失败 {f.name}: {e}")

    # ── CRUD ───────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        description: str = "",
        icon: str = "🤖",
        agents: list[dict[str, Any]] | None = None,
        routing_mode: str = "auto",
        collaboration: bool = False,
        shared_memory: bool = False,
        workspace_id: str = "default",
        owner_id: str = "",
    ) -> DigitalEmployee:
        now = time.time()
        emp = DigitalEmployee(
            name=name,
            description=description,
            icon=icon,
            agents=[EmployeeAgent.from_dict(a) for a in (agents or [])],
            routing_mode=routing_mode,
            collaboration=collaboration,
            shared_memory=shared_memory,
            workspace_id=workspace_id,
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
        )
        self._employees[emp.id] = emp
        self._save_one(emp)
        logger.info(f"[DigitalEmployee] 创建: {emp.name} ({emp.id})")
        return emp

    def list(self, workspace_id: str | None = None) -> list[DigitalEmployee]:
        result = list(self._employees.values())
        if workspace_id:
            result = [e for e in result if e.workspace_id == workspace_id]
        result.sort(key=lambda e: e.updated_at, reverse=True)
        return result

    def get(self, emp_id: str) -> DigitalEmployee | None:
        return self._employees.get(emp_id)

    def update(self, emp_id: str, **kwargs: Any) -> DigitalEmployee | None:
        emp = self._employees.get(emp_id)
        if emp is None:
            return None
        for key, value in kwargs.items():
            if hasattr(emp, key):
                if key == "agents" and isinstance(value, list):
                    value = [EmployeeAgent.from_dict(a) if isinstance(a, dict) else a for a in value]
                setattr(emp, key, value)
        self._save_one(emp)
        return emp

    def delete(self, emp_id: str) -> bool:
        emp = self._employees.pop(emp_id, None)
        if emp is None:
            return False
        try:
            self._employee_path(emp_id).unlink(missing_ok=True)
        except Exception:
            pass
        logger.info(f"[DigitalEmployee] 删除: {emp.name} ({emp_id})")
        return True

    # ── 智能路由 ───────────────────────────────────────────────────────

    def route(
        self,
        emp_id: str,
        query: str,
    ) -> tuple[str | None, str]:
        """
        根据用户 query 路由到最合适的 Agent。

        返回 (profile_id, reason)。
        当前实现：关键词匹配 + 优先级排序。
        """
        emp = self._employees.get(emp_id)
        if not emp or not emp.agents:
            return None, "数字员工无可用 Agent"

        if emp.routing_mode == "manual":
            # 手动模式：返回优先级最高的
            best = min(emp.agents, key=lambda a: a.priority)
            return best.profile_id, f"手动模式 → {best.role_label or best.profile_id}"

        # 自动模式：加载 AgentProfile 信息做匹配
        from mclaw.agents.profile import get_profile_store

        store = get_profile_store()
        scored: list[tuple[int, EmployeeAgent, str]] = []

        query_lower = query.lower()

        for agent in emp.agents:
            profile = store.get(agent.profile_id)
            if profile is None:
                continue

            score = 0
            profile_name = (profile.name or "").lower()
            profile_desc = (profile.description or "").lower()
            role_label = (agent.role_label or "").lower()

            # 角色标签匹配
            if role_label and any(w in query_lower for w in role_label.split()):
                score += 30

            # Agent 名称匹配
            if profile_name and profile_name in query_lower:
                score += 20

            # 描述匹配（词级别）
            desc_words = set(profile_desc.split())
            query_words = set(query_lower.split())
            overlap = desc_words & query_words
            score += len(overlap) * 3

            # 角色标签词匹配
            label_words = set(role_label.split())
            score += len(label_words & query_words) * 5

            # 优先级加成（priority 越小越好 → 分数越高）
            score += (10 - min(agent.priority, 10)) * 2

            scored.append((score, agent, profile_name))

        if not scored:
            return None, "无可匹配的 Agent"

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_agent, best_name = scored[0]

        reason = f"路由到 {best_agent.role_label or best_name} (score={best_score})"
        logger.info(f"[DigitalEmployee] {reason} query={query[:50]}")
        return best_agent.profile_id, reason


# 全局单例
_manager: DigitalEmployeeManager | None = None


def get_digital_employee_manager() -> DigitalEmployeeManager:
    global _manager
    if _manager is None:
        _manager = DigitalEmployeeManager()
    return _manager
