"""
数字员工数据模型
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class EmployeeAgent:
    """数字员工中的单个 Agent 配置"""
    profile_id: str = ""           # AgentProfile.id
    role_label: str = ""           # 在这个数字员工中的角色标签，如"文档专家"
    priority: int = 0              # 优先级（越小越高，用于路由排序）

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "role_label": self.role_label,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EmployeeAgent":
        return cls(
            profile_id=d.get("profile_id", ""),
            role_label=d.get("role_label", ""),
            priority=d.get("priority", 0),
        )


@dataclass
class DigitalEmployee:
    """数字员工：打包多个 Agent + 智能路由"""
    id: str = field(default_factory=_short_uuid)
    name: str = ""
    description: str = ""
    icon: str = "🤖"               # emoji 图标
    agents: list[EmployeeAgent] = field(default_factory=list)
    routing_mode: str = "auto"     # auto | manual
    collaboration: bool = False    # 是否允许多 Agent 协作
    shared_memory: bool = False    # 是否共享子 Agent 的记忆
    workspace_id: str = "default"
    owner_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "agents": [a.to_dict() for a in self.agents],
            "routing_mode": self.routing_mode,
            "collaboration": self.collaboration,
            "shared_memory": self.shared_memory,
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DigitalEmployee":
        agents = [EmployeeAgent.from_dict(a) for a in d.get("agents", [])]
        return cls(
            id=d.get("id", _short_uuid()),
            name=d.get("name", ""),
            description=d.get("description", ""),
            icon=d.get("icon", "🤖"),
            agents=agents,
            routing_mode=d.get("routing_mode", "auto"),
            collaboration=d.get("collaboration", False),
            shared_memory=d.get("shared_memory", False),
            workspace_id=d.get("workspace_id", "default"),
            owner_id=d.get("owner_id", ""),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
        )
