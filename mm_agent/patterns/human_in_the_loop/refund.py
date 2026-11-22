"""Human-in-the-Loop 示例的业务域：订单/客户/支付 mock 与退款政策判定。

「是否该退款」这类判定必须可审计、可复现，所以用代码实现而不是交给模型；
模型只负责起草对客户可见的文案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...llm import Rule
from ...tools import ToolRegistry, tool

# 退款政策口径（示例值，生产环境应来自配置或规则引擎）
REFUND_THRESHOLD = 5000
REFUND_WINDOW_DAYS = 30


class MockCrm:
    """内存里的假订单与支付系统，用来观察「有没有真的产生副作用」。"""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {
            "O-7788": {
                "order_id": "O-7788",
                "customer_id": "C-1024",
                "amount": 8600,
                "days_since_order": 47,
                "status": "已签收",
            }
        }
        self.customers: dict[str, dict[str, Any]] = {
            "C-1024": {"customer_id": "C-1024", "vip": True, "refunds_before": 0, "risk_tag": "无"}
        }
        self.refunds: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def order_of(self, customer_id: str) -> dict[str, Any]:
        for order in self.orders.values():
            if order["customer_id"] == customer_id:
                return order
        raise KeyError(f"客户 {customer_id} 没有订单")


def build_registry(crm: MockCrm) -> ToolRegistry:
    @tool(
        name="get_order",
        description="查询订单详情（金额、下单时间、状态）。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
    )
    def get_order(order_id: str) -> dict[str, Any]:
        if order_id not in crm.orders:
            raise KeyError(f"订单不存在：{order_id}")
        return crm.orders[order_id]

    @tool(
        name="get_customer",
        description="查询客户画像（VIP、历史退款次数、风险标签）。",
        parameters={
            "type": "object",
            "properties": {"customer_id": {"type": "string", "description": "客户号"}},
            "required": ["customer_id"],
        },
    )
    def get_customer(customer_id: str) -> dict[str, Any]:
        if customer_id not in crm.customers:
            raise KeyError(f"客户不存在：{customer_id}")
        return crm.customers[customer_id]

    @tool(
        name="payment_refund",
        description="向支付网关发起退款；不可撤销。",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "amount": {"type": "integer", "description": "退款金额（元）"},
            },
            "required": ["order_id", "amount"],
        },
    )
    def payment_refund(order_id: str, amount: int) -> str:
        refund_id = f"RF-{1001 + len(crm.refunds)}"
        crm.refunds.append({"order_id": order_id, "amount": amount, "refund_id": refund_id})
        return f"已退款 {amount} 元至原支付渠道，流水号 {refund_id}"

    @tool(
        name="send_customer_message",
        description="向客户发送站内信（对外沟通，客户可见）。",
        parameters={
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "客户号"},
                "text": {"type": "string", "description": "消息正文"},
            },
            "required": ["customer_id", "text"],
        },
    )
    def send_customer_message(customer_id: str, text: str) -> str:
        crm.messages.append({"customer_id": customer_id, "text": text})
        return f"已向 {customer_id} 发送站内信：{text}"

    return ToolRegistry([get_order, get_customer, payment_refund, send_customer_message])


@dataclass(frozen=True)
class Assessment:
    """政策判定结果：风险等级、依据、建议动作。"""

    risk: str
    reasons: tuple[str, ...]
    proposed_action: str
    requires_human: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "reasons": list(self.reasons),
            "proposed_action": self.proposed_action,
            "requires_human": self.requires_human,
        }


def assess(order: dict[str, Any], customer: dict[str, Any]) -> Assessment:
    """退款政策判定。规则用代码而不是模型，因为这一步要可审计、可复现。"""

    reasons: list[str] = []
    high_risk = False
    if order["amount"] >= REFUND_THRESHOLD:
        reasons.append(f"金额 {order['amount']} 元 ≥ 高风险阈值 {REFUND_THRESHOLD} 元")
        high_risk = True
    if order["days_since_order"] > REFUND_WINDOW_DAYS:
        reasons.append(f"下单已 {order['days_since_order']} 天，超出 {REFUND_WINDOW_DAYS} 天无理由窗口")
        high_risk = True
    if customer["vip"]:
        reasons.append("VIP 客户，历史无投诉")
    if customer["refunds_before"] == 0:
        reasons.append("历史退款 0 次，无异常退款模式")
    if customer["risk_tag"] != "无":
        reasons.append(f"存在风险标签：{customer['risk_tag']}")
        high_risk = True
    return Assessment(
        risk="high" if high_risk else "low",
        reasons=tuple(reasons),
        proposed_action=f"调用支付网关退款 {order['amount']} 元（不可撤销）",
        # 涉钱动作一律要人拍板，与风险分档无关。
        requires_human=True,
    )


MESSAGE_DRAFT = (
    "您好，您的退款申请已通过审核，8600 元将在 1~3 个工作日退回原支付渠道；"
    "本次超出售后期我们按 VIP 权益特殊处理，感谢您的耐心等待。"
)

MESSAGE_FALLBACK = "无法生成回复。"


def message_rules() -> tuple[Rule, ...]:
    return (Rule(name="draft-message", pattern=r"(?s)起草", reply=MESSAGE_DRAFT),)
