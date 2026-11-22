"""模式 2：ReAct（推理-行动循环） —— 包入口。

实现见 :mod:`mm_agent.patterns.react.agent`；
``python -m mm_agent.patterns.react`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import DEFAULT_CHANNEL, DEFAULT_TASK, MockCluster, build_registry, run

__all__ = [
    "DEFAULT_CHANNEL",
    "DEFAULT_TASK",
    "MockCluster",
    "build_registry",
    "run",
]
