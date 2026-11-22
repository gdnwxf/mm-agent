"""模式 1：Tool Use（工具调用）。

核心：模型不直接产生事实，而是「选工具 → 给参数 → 读结果」。事实来自工具返回的
Observation，模型只负责决策与措辞。

示例任务：算出 (1234*56)/7，把结果写进标题为 ``calc-demo`` 的笔记，再汇报。

这里刻意让模型第一次调用就用错参数名（``expr`` 而不是 ``expression``），
用来演示「Schema 校验 + 结构化错误 → 模型自我纠正」这条闭环。少了这一环，
工具失败就会静默，模型会拿记忆里的数字硬答——这是最常见的生产事故形态。
"""

from __future__ import annotations

import ast
import json
from typing import Any

from ...llm import LLM, Message, Rule, build_llm
from ...protocol import ProtocolError, parse_step
from ...tools import ToolRegistry, tool
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "用计算器算出 (1234*56)/7，把结果保存到标题为 calc-demo 的笔记，然后汇报。"

SYSTEM_PROMPT = """你是一个只通过工具获取事实的助手。

可用工具：
{tools}

每轮必须严格按下面两种格式之一输出：
1) 需要调用工具时：
Thought: <一句话说明下一步做什么>
Action: <工具名>
Action Input: <JSON 对象>

2) 任务完成时：
Final Answer: <给用户的最终答复>

规则：数字与外部状态必须来自工具返回的 Observation，禁止凭记忆编造。
"""

_FINAL = (
    "Thought: 结果已经算出来并写入笔记，可以汇报了。\n"
    "Final Answer: (1234*56)/7 = 9872，已保存到笔记 calc-demo。"
)


def _eval_node(node: ast.AST) -> float:
    """白名单求值：只接受数字常量与四则运算，不把任意表达式交给解释器。"""

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("只支持数字常量")
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("除数为 0")
            return left / right
    raise ValueError("表达式只支持数字与 + - * /（可加括号）")


def build_registry() -> tuple[ToolRegistry, dict[str, str]]:
    """构造一份独立工具集：笔记存在内存里，示例之间不共享状态。"""

    notes: dict[str, str] = {}

    @tool(
        name="calculator",
        description="计算算术表达式，支持 + - * / 与括号。",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "算术表达式，例如 (1234*56)/7"}},
            "required": ["expression"],
        },
    )
    def calculator(expression: str) -> str:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"表达式语法错误：{exc.msg}") from exc
        return f"{_eval_node(tree.body):g}"

    @tool(
        name="save_note",
        description="把一段文本保存为带标题的笔记，标题相同则覆盖。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题"},
                "content": {"type": "string", "description": "笔记正文"},
            },
            "required": ["title", "content"],
        },
    )
    def save_note(title: str, content: str) -> str:
        notes[title] = content
        return f"笔记已保存：{title}（{len(content)} 字）"

    @tool(
        name="list_notes",
        description="列出已保存笔记的标题与字数。",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def list_notes() -> str:
        if not notes:
            return "暂无笔记"
        return "；".join(f"{title}（{len(text)} 字）" for title, text in notes.items())

    @tool(
        name="read_note",
        description="按标题读取笔记正文。",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string", "description": "笔记标题"}},
            "required": ["title"],
        },
    )
    def read_note(title: str) -> str:
        if title not in notes:
            raise KeyError(f"没有标题为 {title} 的笔记")
        return notes[title]

    return ToolRegistry([calculator, save_note, list_notes, read_note]), notes


def _rules() -> tuple[Rule, ...]:
    """假模型的规则。顺序即优先级：条件更「晚期」的规则必须排在前面。

    因为对话历史会累积，早期出现的错误信息会一直留在提示词里，所以每条规则都要
    用负向断言把「已经走到下一步」的历史排除掉。
    """

    return (
        # 第 1 轮：还没拿到任何 Observation，模型先用错参数名。
        Rule(
            name="first-call-with-wrong-arg",
            pattern=r"(?s)\A(?!.*Observation:)",
            reply=(
                "Thought: 需要先算 (1234 * 56) / 7，调用计算器。\n"
                "Action: calculator\n"
                'Action Input: {"expr": "(1234 * 56) / 7"}'
            ),
        ),
        # 笔记已写入 → 收尾。
        Rule(name="final", pattern=r"(?s)笔记已保存", reply=_FINAL),
        # 校验失败但还没算出结果 → 按错误提示改正参数名。
        Rule(
            name="retry-after-validation-error",
            pattern=r"(?s)缺少必填参数 expression(?!.*Observation: 9872)",
            reply=(
                "Thought: 参数名写错了，Schema 要求 expression，重新调用。\n"
                "Action: calculator\n"
                'Action Input: {"expression": "(1234 * 56) / 7"}'
            ),
        ),
        # 已拿到计算结果 → 写笔记。
        Rule(
            name="save-note",
            pattern=r"(?s)Observation: 9872\b",
            reply=(
                "Thought: 结果是 9872，写入笔记 calc-demo。\n"
                "Action: save_note\n"
                'Action Input: {"title": "calc-demo", "content": "(1234*56)/7 = 9872"}'
            ),
        ),
    )


_FALLBACK = "Thought: 缺少可用的下一步依据。\nFinal Answer: 未能完成任务。"


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    max_steps: int = 6,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    registry, notes = build_registry()
    llm = llm or build_llm(llm_kind, rules=_rules(), fallback=_FALLBACK, label="tool-use")
    trace = trace if trace is not None else Trace()

    trace.add("step", f"任务：{task}", f"可用工具：{', '.join(registry.names)}")
    messages = [
        Message("system", SYSTEM_PROMPT.format(tools=registry.describe())),
        Message("user", f"任务：{task}"),
    ]
    actions: list[dict[str, Any]] = []

    for index in range(1, max_steps + 1):
        reply = llm.chat(messages)
        try:
            step = parse_step(reply)
        except ProtocolError as exc:
            trace.add("error", f"模型输出不符合协议：{exc}")
            messages.append(Message("assistant", reply))
            messages.append(Message("user", f"Observation: 输出格式错误：{exc}。请严格使用 Thought/Action/Action Input 或 Final Answer。"))
            continue

        if step.thought:
            trace.add("thought", step.thought)
        if step.is_final:
            trace.add("final", step.final_answer or "")
            failed = sum(1 for item in actions if not item["ok"])
            return PatternResult(
                pattern="tool-use",
                task=task,
                ok=True,
                output=step.final_answer or "",
                steps=index,
                llm_calls=llm.calls,
                notes=[f"工具调用 {len(actions)} 次，失败 {failed} 次（失败原因是模型可见的结构化错误）"],
                extra={"actions": actions, "notes_store": dict(notes)},
            )

        result = registry.call(step.action or "", step.action_input)
        trace.add("action", f"{step.action} {json.dumps(step.action_input, ensure_ascii=False)}")
        trace.add("observation", result.as_observation())
        actions.append(
            {
                "tool": step.action,
                "arguments": step.action_input,
                "ok": result.ok,
                "output": result.output,
                "error": result.error,
            }
        )
        messages.append(Message("assistant", reply))
        messages.append(Message("user", f"Observation: {result.as_observation()}"))

    trace.add("error", f"达到最大步数 {max_steps} 仍未给出 Final Answer")
    return PatternResult(
        pattern="tool-use",
        task=task,
        ok=False,
        output="",
        steps=max_steps,
        llm_calls=llm.calls,
        notes=["步数护栏触发：工具调用协议下同样需要最大步数兜底"],
        extra={"actions": actions, "notes_store": dict(notes)},
    )
