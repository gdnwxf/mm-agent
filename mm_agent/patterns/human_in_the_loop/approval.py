"""Human-in-the-Loop 的审批通道抽象。

把「闸门 + 审批通道」从流程编排里独立出来：流程只依赖 :class:`Approver` 协议，
演示、单测与生产可以换成不同通道（终端交互、自动化脚本、工单/IM 审批）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Gate:
    """一个等待人工决策的检查点。"""

    key: str
    question: str
    context: str
    risk: str
    reversible: bool
    default_decision: bool = False
    """审批无人响应时的兜底结论；默认拒绝（fail-safe）。"""

    def render(self) -> str:
        return (
            f"问题：{self.question}\n"
            f"风险等级：{self.risk}｜可撤销：{'是' if self.reversible else '否'}\n"
            f"无人响应时默认：{'批准' if self.default_decision else '拒绝'}\n"
            f"上下文：\n{self.context}"
        )


@dataclass(frozen=True)
class Approval:
    approved: bool
    by: str
    reason: str = ""


class Approver(Protocol):
    """审批通道：返回 ``None`` 表示「无人响应」。"""

    def decide(self, gate: Gate) -> Approval | None: ...


@dataclass
class AutoApprover:
    """确定性审批者，用于演示与单测；真实系统里换成工单/IM 审批。"""

    approved: bool = True
    by: str = "demo-operator"
    reason: str = "演示用自动审批"

    def decide(self, gate: Gate) -> Approval | None:
        return Approval(approved=self.approved, by=self.by, reason=self.reason)


@dataclass
class TimeoutApprover:
    """模拟审批人始终不响应，用来验证 fail-safe 拒绝路径。"""

    def decide(self, gate: Gate) -> Approval | None:
        return None


class CliApprover:
    """把审批请求打到终端，等人输入。"""

    def decide(self, gate: Gate) -> Approval | None:
        print("\n------ 需要人工审批 ------")
        print(gate.render())
        answer = input("批准执行？[y/N] ").strip().lower()
        approved = answer in {"y", "yes"}
        return Approval(approved=approved, by="cli", reason="终端审批")
