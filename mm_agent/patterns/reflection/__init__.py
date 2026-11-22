"""模式 3：Reflection（自我反思） —— 包入口。

实现见 :mod:`mm_agent.patterns.reflection.agent`；
``python -m mm_agent.patterns.reflection`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import DEFAULT_TASK, TESTS, run

__all__ = [
    "DEFAULT_TASK",
    "TESTS",
    "run",
]
