"""模式 2：ReAct（Reason + Act）。

核心循环：``Thought → Action → Observation → Thought → ...``，直到模型能给出
``Final Answer``。与 Plan-and-Execute 的区别是「边走边看」：下一步取决于上一步
真实返回的结果，而不是事先排好的计划。

示例任务：api-gateway 大量返回 502，定位原因并恢复服务。

示例同时给出两条护栏（原文点名的两个坑）：
- 最大步数：防止模型无限循环；
- 重复动作检测：同一工具 + 同一参数出现 3 次即判定死循环并终止。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ...llm import LLM, Message, Rule, build_llm
from ...protocol import ProtocolError, parse_step
from ...tools import ToolRegistry, tool
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "api-gateway 大量返回 502，定位原因并恢复服务。"
DEFAULT_CHANNEL = "oncall"

SYSTEM_PROMPT = """你是一个线上排障助手，用下面这些工具逐步排查：

{tools}

每轮输出格式：
Thought: <根据已有 Observation 说明下一步判断>
Action: <工具名>
Action Input: <JSON 对象>

排查结束、服务恢复后输出：
Final Answer: <结论：原因 + 已执行的动作 + 后续建议>
"""


class MockCluster:
    """内存里的假服务集群，每次运行独立构造。"""

    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {
            "api-gateway": {"status": "degraded", "cpu": 98, "memory": 61, "error_rate": 12.0, "restarts": 0},
            "auth-service": {"status": "healthy", "cpu": 41, "memory": 55, "error_rate": 0.3, "restarts": 3},
            "order-service": {"status": "healthy", "cpu": 52, "memory": 63, "error_rate": 0.6, "restarts": 1},
        }
        self.logs: dict[str, list[str]] = {
            "api-gateway": [
                "2026-11-22T10:02:11Z WARN  upstream auth-service latency p99=1.8s",
                "2026-11-22T10:02:19Z ERROR connection pool exhausted: active=200 max=200",
                "2026-11-22T10:02:19Z ERROR upstream timeout after 3000ms",
                "2026-11-22T10:03:02Z ERROR container OOMKilled: memory limit 512Mi exceeded",
                "2026-11-22T10:03:05Z WARN  circuit breaker open for auth-service",
            ],
            "auth-service": ["2026-11-22T10:01:40Z INFO  token cache hit ratio=0.93"],
        }
        self.notifications: list[tuple[str, str]] = []

    def require(self, service: str) -> dict[str, Any]:
        if service not in self.services:
            raise KeyError(f"未知服务 {service}；已知服务：{', '.join(self.services)}")
        return self.services[service]

    def describe(self, service: str) -> str:
        state = self.require(service)
        return (
            f"{service}: status={state['status']} cpu={state['cpu']}% memory={state['memory']}% "
            f"error_rate={state['error_rate']}% restarts={state['restarts']}"
        )


def build_registry(cluster: MockCluster) -> ToolRegistry:
    @tool(
        name="service_status",
        description="查询服务的健康状态指标。",
        parameters={
            "type": "object",
            "properties": {"service": {"type": "string", "description": "服务名，例如 api-gateway"}},
            "required": ["service"],
        },
    )
    def service_status(service: str) -> str:
        return cluster.describe(service)

    @tool(
        name="fetch_logs",
        description="读取服务最近的日志行。",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "服务名"},
                "lines": {"type": "integer", "description": "最多返回多少行，默认 50"},
            },
            "required": ["service"],
        },
    )
    def fetch_logs(service: str, lines: int = 50) -> str:
        cluster.require(service)
        rows = cluster.logs.get(service, [])
        if not rows:
            return f"{service} 最近 {lines} 行没有日志"
        return f"{service} 最近 {min(lines, len(rows))} 行日志：\n" + "\n".join(rows[:lines])

    @tool(
        name="restart_service",
        description="滚动重启服务；会造成秒级流量抖动。",
        parameters={
            "type": "object",
            "properties": {"service": {"type": "string", "description": "服务名"}},
            "required": ["service"],
        },
    )
    def restart_service(service: str) -> str:
        state = cluster.require(service)
        state["status"] = "healthy"
        state["cpu"] = 35
        state["memory"] = 42
        state["error_rate"] = 0.4
        state["restarts"] = int(state["restarts"]) + 1
        return f"restart {service} 已执行：{cluster.describe(service)}"

    @tool(
        name="notify",
        description="向指定通道发送通知。",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "通知通道，例如 oncall"},
                "text": {"type": "string", "description": "通知正文"},
            },
            "required": ["channel", "text"],
        },
    )
    def notify(channel: str, text: str) -> str:
        cluster.notifications.append((channel, text))
        return f"已向 {channel} 发送通知：{text[:60]}"

    return ToolRegistry([service_status, fetch_logs, restart_service, notify])


def _rules() -> tuple[Rule, ...]:
    return (
        Rule(
            name="first-observation",
            pattern=r"(?s)\A(?!.*Observation:)",
            reply=(
                "Thought: 先看 api-gateway 当前状态，确认是资源问题还是依赖问题。\n"
                "Action: service_status\n"
                'Action Input: {"service": "api-gateway"}'
            ),
        ),
        Rule(
            name="final",
            pattern=r"(?s)已向 \S+ 发送通知",
            reply=(
                "Thought: 服务已恢复且已通知值班，可以收尾。\n"
                "Final Answer: api-gateway 的 502 由连接池耗尽（active=200/max=200）叠加容器 OOMKilled 引起；"
                "已滚动重启，状态恢复 healthy（cpu=35%、error_rate=0.4%），并已通知 oncall。"
                "建议：调大连接池与内存上限，并为该依赖加熔断降级。"
            ),
        ),
        Rule(
            name="notify-after-restart",
            pattern=r"(?s)status=healthy",
            reply=(
                "Thought: 服务已恢复，把结论同步到值班通道。\n"
                "Action: notify\n"
                'Action Input: {"channel": "oncall", "text": "api-gateway 已重启恢复，根因：连接池耗尽 + OOMKilled"}'
            ),
        ),
        Rule(
            name="restart",
            pattern=r"(?s)connection pool exhausted(?!.*已执行)",
            reply=(
                "Thought: 日志显示连接池耗尽并触发 OOMKilled，重启可以立刻恢复，但根因需要后续调参。\n"
                "Action: restart_service\n"
                'Action Input: {"service": "api-gateway"}'
            ),
        ),
        Rule(
            name="fetch-logs",
            pattern=r"(?s)status=degraded(?!.*connection pool exhausted)",
            reply=(
                "Thought: 状态是 degraded 且 CPU 98%，需要看日志确认是代码问题还是资源耗尽。\n"
                "Action: fetch_logs\n"
                'Action Input: {"service": "api-gateway", "lines": 50}'
            ),
        ),
    )


_FALLBACK = "Thought: 没有可用的下一步依据。\nFinal Answer: 排查中断，请人工介入。"


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    max_steps: int = 8,
    repeat_limit: int = 3,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    cluster = MockCluster()
    registry = build_registry(cluster)
    llm = llm or build_llm(llm_kind, rules=_rules(), fallback=_FALLBACK, label="react")
    trace = trace if trace is not None else Trace()

    trace.add("step", f"任务：{task}", f"可用工具：{', '.join(registry.names)}｜护栏：最大 {max_steps} 步、同动作最多 {repeat_limit} 次")
    messages = [
        Message("system", SYSTEM_PROMPT.format(tools=registry.describe())),
        Message("user", f"任务：{task}"),
    ]
    actions: list[dict[str, Any]] = []
    seen: Counter[tuple[str, str]] = Counter()

    for index in range(1, max_steps + 1):
        reply = llm.chat(messages)
        try:
            step = parse_step(reply)
        except ProtocolError as exc:
            trace.add("error", f"模型输出不符合协议：{exc}")
            messages.append(Message("assistant", reply))
            messages.append(Message("user", f"Observation: 输出格式错误：{exc}。请严格按 Thought/Action/Action Input 或 Final Answer 输出。"))
            continue

        if step.thought:
            trace.add("thought", step.thought)
        if step.is_final:
            trace.add("final", step.final_answer or "")
            return PatternResult(
                pattern="react",
                task=task,
                ok=True,
                output=step.final_answer or "",
                steps=index,
                llm_calls=llm.calls,
                notes=[f"步数 {index}/{max_steps}；每一步都是一次完整模型调用，成本随步数线性增长"],
                extra={"actions": actions, "notifications": list(cluster.notifications)},
            )

        seen[step.fingerprint] += 1
        if seen[step.fingerprint] >= repeat_limit:
            trace.add("error", f"动作 {step.action} 以相同参数重复 {seen[step.fingerprint]} 次，判定为死循环并终止")
            return _abort(task, llm, actions, index, "重复动作护栏触发")

        result = registry.call(step.action or "", step.action_input)
        trace.add("action", f"{step.action} {json.dumps(step.action_input, ensure_ascii=False)}")
        trace.add("observation", result.as_observation())
        actions.append({"tool": step.action, "arguments": step.action_input, "ok": result.ok, "output": result.output})
        messages.append(Message("assistant", reply))
        messages.append(Message("user", f"Observation: {result.as_observation()}"))

    trace.add("error", f"达到最大步数 {max_steps} 仍未给出 Final Answer")
    return _abort(task, llm, actions, max_steps, f"最大步数护栏触发（{max_steps} 步）")


def _abort(task: str, llm: LLM, actions: list[dict[str, Any]], steps: int, reason: str) -> PatternResult:
    return PatternResult(
        pattern="react",
        task=task,
        ok=False,
        output="",
        steps=steps,
        llm_calls=llm.calls,
        notes=[f"终止原因：{reason}"],
        extra={"actions": actions},
    )
