"""在子进程里运行候选代码与单测。

Reflection 模式需要一个「不会说谎的批评者」：把可机器判定的事实（单测结果）
交给确定性执行，模型只负责根据事实改写代码。子进程 + 超时保证候选代码即使
死循环也不会拖住 Agent 本体。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class TestRun:
    """一次单测执行结果。"""

    ok: bool
    ran: int
    passed: int
    failed: int
    output: str
    timed_out: bool = False
    workdir: Path | None = None
    """``keep=True`` 时保留的临时目录，便于人工查看失败现场。"""

    def score(self) -> float:
        if self.ran <= 0:
            return 0.0
        return self.passed / self.ran

    def critique(self, *, limit: int = 2000) -> str:
        """渲染成反思提示词里的失败证据。"""

        if self.timed_out:
            return f"测试执行超时：通过 {self.passed}/{self.ran}。请检查是否出现死循环。"
        tail = self.output.strip()[-limit:]
        return f"测试结果：通过 {self.passed}/{self.ran}（失败 {self.failed}）\n--- 单测输出 ---\n{tail}"


def run_unit_tests(
    source: str,
    tests: str,
    *,
    timeout: float = 10.0,
    keep: bool = False,
) -> TestRun:
    """把候选实现与单测写入临时目录后执行，返回结构化结果。

    ``source`` 落到 ``solution.py``，``tests`` 落到 ``test_solution.py``，
    因此单测里用 ``from solution import ...`` 引用候选实现。
    """

    workdir = Path(tempfile.mkdtemp(prefix="mm-agent-reflect-"))
    try:
        (workdir / "solution.py").write_text(source + "\n", encoding="utf-8")
        (workdir / "test_solution.py").write_text(tests + "\n", encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "-v", "test_solution"],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result = _parse_result(completed.returncode, completed.stdout + completed.stderr)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            result = TestRun(ok=False, ran=0, passed=0, failed=0, output=output, timed_out=True)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
    return replace(result, workdir=workdir) if keep else result


def _parse_result(returncode: int, output: str) -> TestRun:
    ran_match = re.search(r"Ran (\d+) tests?", output)
    ran = int(ran_match.group(1)) if ran_match else 0
    failed = 0
    failed_match = re.search(r"FAILED \(([^)]*)\)", output)
    if failed_match:
        failed = sum(int(count) for _, count in re.findall(r"(failures|errors)=(\d+)", failed_match.group(1)))
    if ran and not failed and returncode != 0:
        # 非 failfast 的异常退出（例如解释器错误）也必须有失败计数，避免被误判为通过。
        failed = 1
    passed = max(ran - failed, 0)
    return TestRun(ok=returncode == 0, ran=ran, passed=passed, failed=failed, output=output)
