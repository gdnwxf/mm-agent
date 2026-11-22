"""模式 1：Tool Use（工具调用） —— 包入口。

实现见 :mod:`mm_agent.patterns.tool_use.agent`；
``python -m mm_agent.patterns.tool_use`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import DEFAULT_TASK, build_registry, run

__all__ = [
    "DEFAULT_TASK",
    "build_registry",
    "run",
]
