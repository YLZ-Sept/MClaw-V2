"""
数字员工 (Digital Employee) 模块

数字员工 = 多个 Agent 的组合 + 智能路由。
用户与数字员工对话时，系统自动分析意图并将任务路由到最合适的 Agent。
"""

from .models import DigitalEmployee, EmployeeAgent
from .manager import DigitalEmployeeManager

__all__ = ["DigitalEmployee", "EmployeeAgent", "DigitalEmployeeManager"]
