"""模式 6：Sequential Workflows（顺序工作流） —— 包入口。

实现见 :mod:`mm_agent.patterns.sequential.agent`；
``python -m mm_agent.patterns.sequential`` 可单跑该示例。
"""

from __future__ import annotations

from .agent import (
    DEFAULT_TASK,
    Draft,
    FinalPost,
    KeywordBrief,
    Outline,
    SeoReport,
    Step,
    build_pipeline,
    run,
    run_pipeline,
)

__all__ = [
    "DEFAULT_TASK",
    "Draft",
    "FinalPost",
    "KeywordBrief",
    "Outline",
    "SeoReport",
    "Step",
    "build_pipeline",
    "run",
    "run_pipeline",
]
