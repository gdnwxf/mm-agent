"""工具注册与调用。

工具是 Agent 唯一的外部副作用入口，因此这里承担三件事：

1. 用 JSON Schema 声明参数（模型据此决定怎么调）；
2. 执行前校验参数，用结构化错误替代静默失败（模型能读懂并自我纠正）；
3. 把工具异常收敛成 :class:`ToolResult`，让调用方永远不必 ``try``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


class ToolError(RuntimeError):
    """工具层面的错误（未知工具、重名注册等），由调用方按需处理。"""


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。``ok=False`` 时 ``error`` 是给模型看的可读原因。"""

    ok: bool
    output: str = ""
    error: str | None = None

    def as_observation(self) -> str:
        return self.output if self.ok else f"ERROR: {self.error}"

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "error": self.error}


@dataclass(frozen=True)
class Tool:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def signature(self) -> str:
        properties = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        parts = []
        for key, spec in properties.items():
            kind = spec.get("type", "any")
            mark = "，必填" if key in required else ""
            parts.append(f"{key}({kind}{mark})")
        return f"{self.name}({', '.join(parts)})"


_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Callable[..., Any]], Tool]:
    """把普通函数声明成 :class:`Tool` 的装饰器。"""

    def wrap(handler: Callable[..., Any]) -> Tool:
        return Tool(name=name, description=description, parameters=parameters, handler=handler)

    return wrap


class ToolRegistry:
    """工具集合。

    :meth:`call` 不抛异常：未知工具、参数错误、执行失败都返回 ``ok=False`` 的
    :class:`ToolResult`，这正是「工具失败不能静默」的落地方式。
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for item in tools:
            self.register(item)

    def register(self, item: Tool) -> Tool:
        if item.name in self._tools:
            raise ToolError(f"工具重名：{item.name}")
        self._tools[item.name] = item
        return item

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"未知工具 {name!r}；可用工具：{', '.join(self._tools) or '无'}") from exc

    def describe(self, *, only: Iterable[str] | None = None) -> str:
        """渲染成提示词里的工具清单。``only`` 用于多 Agent 场景下的角色隔离。"""

        selected = [self._tools[name] for name in (only if only is not None else self._tools) if name in self._tools]
        if not selected:
            return "（无可用工具）"
        lines = []
        for item in selected:
            schema = json.dumps(item.parameters, ensure_ascii=False)
            lines.append(f"- {item.name}：{item.description}\n  参数 schema：{schema}")
        return "\n".join(lines)

    def openai_schema(self, *, only: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """转成原生 function calling 的 tools 字段，便于替换成真实模型。"""

        selected = [self._tools[name] for name in (only if only is not None else self._tools) if name in self._tools]
        return [
            {
                "type": "function",
                "function": {"name": item.name, "description": item.description, "parameters": item.parameters},
            }
            for item in selected
        ]

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        try:
            item = self.get(name)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        payload = dict(arguments or {})
        problems = self._validate(item, payload)
        if problems:
            return ToolResult(ok=False, error=f"参数校验失败：{'；'.join(problems)}。工具签名：{item.signature()}")
        try:
            output = item.handler(**payload)
        except Exception as exc:  # 工具内部异常同样要转成模型可读的错误
            return ToolResult(ok=False, error=f"工具 {name} 执行异常：{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, output=_stringify(output))

    def _validate(self, item: Tool, payload: dict[str, Any]) -> list[str]:
        properties: dict[str, Any] = item.parameters.get("properties", {})
        required = list(item.parameters.get("required", []))
        problems: list[str] = []

        for key in required:
            if key not in payload or payload[key] in (None, ""):
                problems.append(f"缺少必填参数 {key}")
        for key in payload:
            if key not in properties:
                problems.append(f"未知参数 {key}")
                continue
            expected = properties[key].get("type")
            if expected in _PY_TYPES:
                value = payload[key]
                # bool 是 int 的子类，需要显式排除，否则 True 会被当成合法的 integer。
                if expected in {"integer", "number"} and isinstance(value, bool):
                    problems.append(f"参数 {key} 期望 {expected}，实际 boolean")
                elif not isinstance(value, _PY_TYPES[expected]):
                    problems.append(f"参数 {key} 期望 {expected}，实际 {type(value).__name__}")
        return problems


def _stringify(output: Any) -> str:
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)
