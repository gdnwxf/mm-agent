"""文本协议解析。

真实生产系统通常用模型原生的 function calling；示例集统一改用文本协议
（``Thought:`` / ``Action:`` / ``Action Input:`` / ``Final Answer:``），
好处是每一步都能原样打印出来，也能用假模型精确复现。解析失败一律抛
:class:`ProtocolError`，由模式层捕获并决定重试还是终止。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class ProtocolError(ValueError):
    """模型输出不符合约定的文本协议。"""


@dataclass(frozen=True)
class AgentStep:
    """一次模型输出的结构化视图。"""

    raw: str
    thought: str | None = None
    action: str | None = None
    action_input: dict[str, Any] = field(default_factory=dict)
    final_answer: str | None = None

    @property
    def is_final(self) -> bool:
        return self.final_answer is not None

    @property
    def fingerprint(self) -> tuple[str, str]:
        """动作指纹，用于识别「反复调用同一工具」的死循环。"""

        try:
            args = json.dumps(self.action_input, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = repr(self.action_input)
        return (self.action or "", args)


def _first(pattern: str, text: str, *, flags: int = re.DOTALL) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None


def parse_step(text: str) -> AgentStep:
    """解析一段 ReAct 风格的模型输出。

    允许只含 ``Final Answer``，也允许只含 ``Action``；两者都没有则视为协议错误。
    """

    thought = _first(r"Thought:\s*(.*?)(?=\n(?:Action|Action Input|Final Answer)\s*:|\Z)", text)
    final_answer = _first(r"Final Answer:\s*(.*)", text)
    action = _first(r"Action:\s*([A-Za-z_][\w.\-]*)", text)
    action_input = _parse_action_input(text)

    if final_answer is None and action is None:
        raise ProtocolError(f"既没有 Action 也没有 Final Answer：{text.strip()[:120]!r}")
    return AgentStep(raw=text, thought=thought, action=action, action_input=action_input, final_answer=final_answer)


def _parse_action_input(text: str) -> dict[str, Any]:
    match = re.search(r"Action Input:\s*(.+)", text, re.DOTALL)
    if match is None:
        return {}
    payload = match.group(1)
    # 截断到后续协议字段，避免把 Observation 等段落吃进参数里。
    payload = re.split(r"\n(?=(?:Thought|Observation|Final Answer|Action)\s*:)", payload)[0].strip()
    if not payload:
        return {}
    if payload.startswith("{"):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Action Input 不是合法 JSON：{exc}: {payload[:120]!r}") from exc
        if not isinstance(parsed, dict):
            raise ProtocolError(f"Action Input 必须是 JSON 对象，实际为 {type(parsed).__name__}")
        return parsed
    return {"input": payload}


def extract_code(text: str, language: str = "python") -> str:
    """取出围栏代码块内容；没有围栏时按整段文本处理。"""

    match = re.search(rf"```(?:{language})?[ \t]*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def extract_json(text: str) -> Any:
    """从模型输出中提取第一个合法 JSON 值（优先围栏块，其次花括号配平段）。"""

    candidates: list[str] = []
    match = re.search(r"```(?:json)?[ \t]*\n(.*?)```", text, re.DOTALL)
    if match:
        candidates.append(match.group(1).strip())
    balanced = _balanced_object(text)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ProtocolError(f"没有找到合法 JSON：{text.strip()[:160]!r}")


def _balanced_object(text: str) -> str | None:
    """截取第一个花括号配平的片段，字符串与转义字符不计入层级。"""

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
