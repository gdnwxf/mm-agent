"""模式 5：Multi-Agent Collaboration（多智能体协作） —— 包入口。

实现见 :mod:`mm_agent.patterns.multi_agent.agent`；
``python -m mm_agent.patterns.multi_agent`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import (
    Blackboard,
    DEFAULT_TASK,
    DRAFT_V1,
    DRAFT_V2,
    MIN_CITATIONS,
    MockSearch,
    WORD_LIMIT,
    build_registry,
    rubric,
    run,
)

__all__ = [
    "Blackboard",
    "DEFAULT_TASK",
    "DRAFT_V1",
    "DRAFT_V2",
    "MIN_CITATIONS",
    "MockSearch",
    "WORD_LIMIT",
    "build_registry",
    "rubric",
    "run",
]
