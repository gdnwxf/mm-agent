"""JSON 状态存储：让 Agent 能跨进程暂停与恢复。

Planning 的断点续跑、Human-in-the-Loop 的等待人工审批都需要把中间状态落到磁盘。
写入采用「同目录临时文件 + ``os.replace``」，保证读到的永远是完整 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


class StateNotFound(FileNotFoundError):
    """期望存在的状态文件不存在。"""


class JsonStateStore:
    """单个 JSON 文件承载一份 Agent 状态。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        if not self.exists():
            raise StateNotFound(f"状态文件不存在：{self.path}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateNotFound(f"状态文件不是合法 JSON：{self.path}：{exc}") from exc
        if not isinstance(data, dict):
            raise StateNotFound(f"状态文件顶层必须是 JSON 对象：{self.path}")
        return data

    def save(self, state: Mapping[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(dict(state), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return self.path

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
