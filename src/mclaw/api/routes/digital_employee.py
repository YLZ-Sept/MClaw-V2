"""
数字员工 API 路由

提供 HTTP API 用于：
- 数字员工 CRUD
- 列出可用 AgentProfile（用于绑定）
- 智能路由测试

所有端点挂载在 /api/digital-employees 下。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mclaw.digital_employee.manager import get_digital_employee_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/digital-employees", tags=["数字员工"])

# ── 请求/响应模型 ─────────────────────────────────────────────────────


class AgentBinding(BaseModel):
    profile_id: str
    role_label: str = ""
    priority: int = 0


class CreateEmployeeRequest(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    agents: list[AgentBinding] = []
    routing_mode: str = "auto"


class UpdateEmployeeRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    agents: list[AgentBinding] | None = None
    routing_mode: str | None = None


class RouteRequest(BaseModel):
    query: str


# ── 权限辅助 ───────────────────────────────────────────────────────────


def _current_owner(request: Request) -> tuple[str, str]:
    user_id = (
        request.headers.get("X-Mclaw-User")
        or getattr(request.state, "user_id", None)
        or "admin"
    )
    workspace_id = (
        request.headers.get("X-Mclaw-Workspace")
        or getattr(request.state, "workspace_id", None)
        or "default"
    )
    return user_id, workspace_id


# ── 路由 ───────────────────────────────────────────────────────────────


@router.get("")
async def list_employees(request: Request):
    """列出当前工作区的所有数字员工"""
    mgr = get_digital_employee_manager()
    user_id, workspace_id = _current_owner(request)
    return [e.to_dict() for e in mgr.list(workspace_id)]


@router.post("", status_code=201)
async def create_employee(request: Request, body: CreateEmployeeRequest):
    """创建数字员工"""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")
    mgr = get_digital_employee_manager()
    user_id, workspace_id = _current_owner(request)
    emp = mgr.create(
        name=body.name.strip(),
        description=body.description.strip(),
        icon=body.icon,
        agents=[a.model_dump() for a in body.agents],
        routing_mode=body.routing_mode,
        workspace_id=workspace_id,
        owner_id=user_id,
    )
    return emp.to_dict()


@router.get("/{emp_id}")
async def get_employee(emp_id: str):
    """获取数字员工详情"""
    mgr = get_digital_employee_manager()
    emp = mgr.get(emp_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    return emp.to_dict()


@router.put("/{emp_id}")
async def update_employee(emp_id: str, body: UpdateEmployeeRequest):
    """更新数字员工"""
    mgr = get_digital_employee_manager()
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.description is not None:
        updates["description"] = body.description.strip()
    if body.icon is not None:
        updates["icon"] = body.icon
    if body.agents is not None:
        updates["agents"] = [a.model_dump() for a in body.agents]
    if body.routing_mode is not None:
        updates["routing_mode"] = body.routing_mode
    if not updates:
        raise HTTPException(status_code=400, detail="无更新内容")
    emp = mgr.update(emp_id, **updates)
    if emp is None:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    return emp.to_dict()


@router.delete("/{emp_id}")
async def delete_employee(emp_id: str):
    """删除数字员工"""
    mgr = get_digital_employee_manager()
    if not mgr.delete(emp_id):
        raise HTTPException(status_code=404, detail="数字员工不存在")
    return {"ok": True}


@router.post("/{emp_id}/route")
async def route_employee(emp_id: str, body: RouteRequest):
    """测试路由：查看某条消息会路由到哪个 Agent"""
    mgr = get_digital_employee_manager()
    emp = mgr.get(emp_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    profile_id, reason = mgr.route(emp_id, body.query)
    return {
        "profile_id": profile_id,
        "reason": reason,
        "available_agents": [
            {"profile_id": a.profile_id, "role_label": a.role_label}
            for a in emp.agents
        ],
    }
