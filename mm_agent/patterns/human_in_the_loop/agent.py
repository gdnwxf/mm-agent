"""模式 7：Human-in-the-Loop（人在回路）。

核心：Agent 在**不可逆或对外**的动作前暂停，把上下文与后果交给人决策；批准后继续，
拒绝则终止且不产生任何副作用。

示例任务：处理客户 C-1024 的 8600 元退款申请（订单已超过 30 天）。

工程要点（也是这个模式最容易做错的地方）：
- **状态落盘**：暂停点写进 JSON，人工审批后由 ``--resume`` 在另一个进程里继续；
- **fail-safe 兜底**：审批无人响应（``TimeoutApprover``）按「拒绝」处理，绝不默认放行；
- **审批上下文完整**：金额、政策判定依据、不可逆性、客户可见后果一并呈现；
- **审计留痕**：每次审批的结论、审批人、理由都进入状态文件与轨迹。

本文件只放流程编排；审批通道见 :mod:`mm_agent.patterns.hitl_approval`，
业务域（订单/客户/支付 mock 与政策判定）见 :mod:`mm_agent.patterns.hitl_refund`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...llm import build_llm
from ...state import JsonStateStore
from ...trace import Trace
from .. import PatternResult
from .approval import Approval, Approver, AutoApprover, CliApprover, Gate, TimeoutApprover
from .refund import MESSAGE_FALLBACK, Assessment, MockCrm, assess, build_registry, message_rules

DEFAULT_TASK = "处理客户 C-1024 的 8600 元退款申请（订单已超过 30 天）。"
DEFAULT_STATE = Path("runs/hitl-state.json")

STAGES: tuple[str, ...] = ("new", "collect", "assess", "gate-refund", "refund", "draft", "gate-notify", "notify", "done")

__all__ = [
    "Approval",
    "Approver",
    "AutoApprover",
    "CliApprover",
    "Gate",
    "TimeoutApprover",
    "assess",
    "build_registry",
    "run",
]


def _resolve_ids(task: str, crm: MockCrm) -> tuple[str, str]:
    customer_match = re.search(r"C-\d+", task)
    order_match = re.search(r"O-\d+", task)
    customer_id = customer_match.group(0) if customer_match else "C-1024"
    if order_match:
        return order_match.group(0), customer_id
    return str(crm.order_of(customer_id)["order_id"]), customer_id


def _rank(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else 0


def run(
    task: str | None = None,
    *,
    llm: Any = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    state_path: str | Path | None = None,
    resume: bool = False,
    approver: Approver | None = None,
    interactive: bool = False,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    store = JsonStateStore(state_path or DEFAULT_STATE)
    crm = MockCrm()
    registry = build_registry(crm)
    trace = trace if trace is not None else Trace()
    llm = llm or build_llm(llm_kind, rules=message_rules(), fallback=MESSAGE_FALLBACK, label="hitl")
    if approver is None and interactive:
        approver = CliApprover()

    state: dict[str, Any] = {
        "task": task,
        "stage": "new",
        "status": "running",
        "facts": {},
        "assessment": {},
        "approvals": [],
        "actions": {},
    }
    if resume:
        if store.exists():
            state = store.load()
            task = str(state.get("task") or task)
            for key, default in (("facts", {}), ("assessment", {}), ("approvals", []), ("actions", {})):
                state.setdefault(key, default)
            trace.add("state", f"从 {store.path} 恢复", f"stage={state['stage']} status={state['status']}")
            if state.get("status") == "rejected":
                trace.add("state", "该申请上一轮已被人工拒绝，直接返回既有结论，不再执行任何动作")
                return _result(task, store, state, ok=False, output="", extra_note="人工拒绝的结论已落盘，续跑不会重新尝试", llm_calls=llm.calls)
        else:
            trace.add("state", f"没有找到状态文件 {store.path}，按全新任务执行")

    if state["stage"] == "new":
        trace.add("step", f"任务：{task}", "策略：涉钱与对外沟通的动作一律先暂停等人工审批")

    order_id, customer_id = _resolve_ids(task, crm)
    order: dict[str, Any] = {}
    customer: dict[str, Any] = {}

    # ---- 阶段 1：收集事实 ----
    if _rank(state["stage"]) < _rank("collect"):
        for tool_name, args in (("get_order", {"order_id": order_id}), ("get_customer", {"customer_id": customer_id})):
            result = registry.call(tool_name, args)
            trace.add("action", f"{tool_name} {json.dumps(args, ensure_ascii=False)}")
            trace.add("observation", result.as_observation())
            if not result.ok:
                return _fail(task, store, state, f"收集事实失败：{result.error}", llm_calls=llm.calls)
            order = json.loads(result.output) if tool_name == "get_order" else order
            customer = json.loads(result.output)
        state.update({"stage": "collect", "facts": {"order": order, "customer": customer}})
        store.save(state)
    else:
        trace.add("state", "跳过已完成阶段 collect")
        order = state["facts"]["order"]
        customer = state["facts"]["customer"]

    # ---- 阶段 2：政策判定 ----
    if _rank(state["stage"]) < _rank("assess"):
        verdict = assess(order, customer)
        state.update({"stage": "assess", "assessment": verdict.as_dict()})
        store.save(state)
        trace.add("plan", f"政策判定：风险={verdict.risk}，需要人工审批={verdict.requires_human}", "\n".join(verdict.reasons) + f"\n建议动作：{verdict.proposed_action}")
    else:
        trace.add("state", "跳过已完成阶段 assess")
        verdict = Assessment(
            risk=str(state["assessment"].get("risk", "high")),
            reasons=tuple(state["assessment"].get("reasons", [])),
            proposed_action=str(state["assessment"].get("proposed_action", "")),
            requires_human=bool(state["assessment"].get("requires_human", True)),
        )

    # ---- 阶段 3：退款审批闸门 ----
    if _rank(state["stage"]) < _rank("gate-refund"):
        gate = Gate(
            key="refund",
            question=f"是否批准退款 {order['amount']} 元？",
            risk=verdict.risk,
            reversible=False,
            default_decision=False,
            context=(
                f"订单 {order['order_id']}｜客户 {customer['customer_id']}｜金额 {order['amount']} 元\n"
                f"下单 {order['days_since_order']} 天｜状态 {order['status']}\n"
                f"判定依据：\n- " + "\n- ".join(verdict.reasons) + "\n"
                f"若批准将执行：{verdict.proposed_action}\n"
                "后果：退款不可撤销；批准后系统还会向客户发送一条站内信（需再次审批）。"
            ),
        )
        paused = _gate(gate, approver, trace, state, store, task, llm_calls=llm.calls)
        if paused is not None:
            return paused
        state["stage"] = "gate-refund"
        store.save(state)
    else:
        trace.add("state", "跳过已完成阶段 gate-refund（已有审批结论）")

    # ---- 阶段 4：执行退款（不可逆副作用）----
    if _rank(state["stage"]) < _rank("refund"):
        result = registry.call("payment_refund", {"order_id": order["order_id"], "amount": int(order["amount"])})
        trace.add("action", f"payment_refund {json.dumps({'order_id': order['order_id'], 'amount': order['amount']}, ensure_ascii=False)}")
        trace.add("observation", result.as_observation())
        if not result.ok:
            return _fail(task, store, state, f"退款执行失败：{result.error}", llm_calls=llm.calls)
        state["actions"]["refund"] = result.output
        state["stage"] = "refund"
        store.save(state)
    else:
        trace.add("state", "跳过已完成阶段 refund（退款已执行，不重复调用支付网关）")

    # ---- 阶段 5：起草对外文案（LLM 唯一参与的环节）----
    if _rank(state["stage"]) < _rank("draft"):
        message = llm.ask(f"起草一条给客户 {customer['customer_id']} 的退款完成通知，不超过 60 字。").strip()
        state["actions"]["message"] = message
        state["stage"] = "draft"
        store.save(state)
        trace.add("message", "对外文案草稿（未经审批不得发送）", message)
    else:
        trace.add("state", "跳过已完成阶段 draft")
        message = str(state["actions"].get("message", ""))

    # ---- 阶段 6：对外沟通审批闸门 ----
    if _rank(state["stage"]) < _rank("gate-notify"):
        gate = Gate(
            key="notify",
            question=f"是否批准向客户 {customer['customer_id']} 发送这条站内信？",
            risk="high",
            reversible=False,
            default_decision=False,
            context=f"消息正文：\n{message}\n\n后果：客户可见、已发送无法撤回。",
        )
        paused = _gate(gate, approver, trace, state, store, task, llm_calls=llm.calls)
        if paused is not None:
            return paused
        state["stage"] = "gate-notify"
        store.save(state)
    else:
        trace.add("state", "跳过已完成阶段 gate-notify（已有审批结论）")

    # ---- 阶段 7：发送消息 ----
    if _rank(state["stage"]) < _rank("notify"):
        result = registry.call("send_customer_message", {"customer_id": customer["customer_id"], "text": message})
        trace.add("action", "send_customer_message")
        trace.add("observation", result.as_observation())
        if not result.ok:
            return _fail(task, store, state, f"发送消息失败：{result.error}", llm_calls=llm.calls)
        state["actions"]["notify"] = result.output
        state["stage"] = "notify"
        store.save(state)
    else:
        trace.add("state", "跳过已完成阶段 notify（消息已发送，不重复发送）")

    state.update({"stage": "done", "status": "done"})
    store.save(state)
    output = f"{state['actions'].get('refund', '')}；{state['actions'].get('notify', '')}"
    trace.add("final", output)
    return _result(
        task,
        store,
        state,
        ok=True,
        output=output,
        extra_note=f"人工审批 {len(state['approvals'])} 次，全部留痕在状态文件里",
        extra={"refunds": crm.refunds, "messages": crm.messages},
        llm_calls=llm.calls,
    )


def _gate(
    gate: Gate,
    approver: Approver | None,
    trace: Trace,
    state: dict[str, Any],
    store: JsonStateStore,
    task: str,
    *,
    llm_calls: int = 0,
) -> PatternResult | None:
    """处理一个审批闸门；返回 PatternResult 表示暂停/终止，返回 None 表示可以继续。"""

    for record in state["approvals"]:
        if record.get("gate") == gate.key:
            trace.add("state", f"闸门 {gate.key} 已有审批结论：{'批准' if record.get('approved') else '拒绝'}")
            if record.get("approved"):
                return None
            return _result(task, store, state, ok=False, output="", extra_note="该闸门已被人工拒绝", llm_calls=llm_calls)

    trace.add("gate", f"到达人工审批闸门：{gate.question}", gate.render())
    decision = approver.decide(gate) if approver is not None else None

    if decision is None and approver is None:
        state.update({"status": "waiting_approval", "pending_gate": gate.key})
        store.save(state)
        trace.add("state", f"本次进程到此结束，状态已落盘：{store.path}", f"人工审批后续跑：mm-agent run human-in-the-loop --resume --approve（或 --reject）")
        return _result(
            task,
            store,
            state,
            ok=False,
            output="",
            extra_note="已暂停等待人工审批——这是该模式的正常状态，不是失败",
            extra={"waiting": True, "gate": gate.key},
            llm_calls=llm_calls,
        )

    if decision is None:
        decision = Approval(
            approved=gate.default_decision,
            by="system",
            reason=f"审批无人响应，按 fail-safe 默认{'批准' if gate.default_decision else '拒绝'}",
        )

    state["approvals"].append(
        {"gate": gate.key, "approved": decision.approved, "by": decision.by, "reason": decision.reason}
    )
    store.save(state)
    trace.add(
        "approval",
        f"{'批准' if decision.approved else '拒绝'}：{gate.question}",
        f"审批人：{decision.by}\n理由：{decision.reason}",
    )
    if not decision.approved:
        state.update({"status": "rejected"})
        store.save(state)
        trace.add("error", "人工拒绝，Agent 终止：不执行副作用")
        return _result(
            task,
            store,
            state,
            ok=False,
            output="",
            extra_note=f"闸门 {gate.key} 被人工拒绝，未产生任何副作用",
            llm_calls=llm_calls,
        )
    return None


def _result(
    task: str,
    store: JsonStateStore,
    state: dict[str, Any],
    *,
    ok: bool,
    output: str,
    extra_note: str,
    extra: dict[str, Any] | None = None,
    llm_calls: int = 0,
) -> PatternResult:
    payload = {
        "stage": state.get("stage"),
        "status": state.get("status"),
        "approvals": state.get("approvals", []),
        "actions": state.get("actions", {}),
        "state_path": str(store.path),
    }
    payload.update(extra or {})
    return PatternResult(
        pattern="human-in-the-loop",
        task=task,
        ok=ok,
        output=output,
        steps=_rank(str(state.get("stage", "new"))),
        llm_calls=llm_calls,
        notes=[extra_note, f"状态文件：{store.path}"],
        extra=payload,
    )


def _fail(task: str, store: JsonStateStore, state: dict[str, Any], reason: str, *, llm_calls: int = 0) -> PatternResult:
    state.update({"status": "failed", "reason": reason})
    store.save(state)
    return _result(task, store, state, ok=False, output="", extra_note=reason, llm_calls=llm_calls)
