"""模式 7：Human-in-the-Loop（人在回路） —— 包入口。

实现见 :mod:`mm_agent.patterns.human_in_the_loop.agent`；
``python -m mm_agent.patterns.human_in_the_loop`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import (
    Approval,
    Approver,
    Assessment,
    AutoApprover,
    CliApprover,
    DEFAULT_STATE,
    DEFAULT_TASK,
    Gate,
    MockCrm,
    STAGES,
    TimeoutApprover,
    assess,
    build_registry,
    run,
)

__all__ = [
    "Approval",
    "Approver",
    "Assessment",
    "AutoApprover",
    "CliApprover",
    "DEFAULT_STATE",
    "DEFAULT_TASK",
    "Gate",
    "MockCrm",
    "STAGES",
    "TimeoutApprover",
    "assess",
    "build_registry",
    "run",
]
