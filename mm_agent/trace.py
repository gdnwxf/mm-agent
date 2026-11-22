"""执行轨迹。

Agent 的可观测性基本等于「每一步的输入输出有没有被记下来」。:class:`Trace`
同时负责终端呈现、事件统计和 JSON 落地，模式层只调用 :meth:`Trace.add`。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

_STYLES: dict[str, str] = {
    "thought": "36",
    "action": "33",
    "observation": "32",
    "critique": "35",
    "plan": "34",
    "step": "34",
    "contract": "90",
    "message": "90",
    "gate": "1;33",
    "approval": "1;33",
    "state": "90",
    "final": "1;32",
    "error": "1;31",
}


@dataclass(frozen=True)
class Event:
    """一条轨迹事件；``kind`` 决定终端配色与统计口径。"""

    index: int
    kind: str
    title: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "kind": self.kind, "title": self.title, "detail": self.detail}


class Trace:
    """按顺序收集并打印 Agent 事件。

    ``stream=None`` 表示只记录不打印（测试与库内调用使用）；``color=None`` 时
    依据 ``stream.isatty()`` 自动决定是否着色。
    """

    def __init__(self, title: str = "", *, stream: TextIO | None = None, color: bool | None = None) -> None:
        self.title = title
        self.stream = stream
        self.color = bool(getattr(stream, "isatty", lambda: False)()) if color is None else color
        self.events: list[Event] = []
        if stream is not None and title:
            stream.write(f"\n=== {title} ===\n")
            stream.flush()

    def add(self, kind: str, title: str, detail: str = "") -> Event:
        event = Event(index=len(self.events) + 1, kind=kind, title=title, detail=detail)
        self.events.append(event)
        if self.stream is not None:
            self._write(event)
        return event

    def _write(self, event: Event) -> None:
        label = event.kind.upper().ljust(11)
        if self.color:
            label = f"\033[{_STYLES.get(event.kind, '0')}m{label}\033[0m"
        self.stream.write(f"[{event.index:02d}] {label} {event.title}\n")
        if event.detail:
            for line in str(event.detail).rstrip().splitlines():
                self.stream.write(f"            | {line}\n")
        self.stream.flush()

    @property
    def counts(self) -> Counter:
        return Counter(event.kind for event in self.events)

    def of_kind(self, kind: str) -> list[Event]:
        return [event for event in self.events if event.kind == kind]

    def summary(self) -> str:
        if not self.events:
            return "事件 0"
        detail = " / ".join(f"{kind} {count}" for kind, count in sorted(self.counts.items()))
        return f"事件 {len(self.events)}（{detail}）"

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "counts": dict(sorted(self.counts.items())),
            "events": [event.as_dict() for event in self.events],
        }

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target


def make_trace(title: str, *, quiet: bool = False) -> Trace:
    """便捷构造：``quiet=True`` 时不向终端输出。"""

    return Trace(title, stream=None if quiet else sys.stdout)
