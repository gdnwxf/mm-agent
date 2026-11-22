"""模式 3：Reflection（自我反思）。

核心：``Generate → Critique → Revise``，循环到满足标准或触顶。

示例任务：实现 ``parse_duration(text)``，把 ``"1h30m"`` 这类时长文本转成秒数，
并通过随附单测。

关键设计：批评者的证据是**真实执行单测的结果**，不是模型随口点评。反思能否收敛，
取决于评审标准是否可判定——「检查一下好不好」只会让循环空转；「这 4 个用例必须通过」
才可执行。整个反思循环跑在子进程里（见 :mod:`mm_agent.pyrun`），候选代码死循环
也不会拖住 Agent。
"""

from __future__ import annotations

from typing import Any

from ...llm import LLM, Rule, build_llm
from ...protocol import extract_code
from ...pyrun import run_unit_tests
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "实现 parse_duration(text)：把 '1h30m' 这类时长文本转成秒数，并通过随附单测。"

SYSTEM_PROMPT = "你是一个 Python 工程师，只输出一个 ```python 代码块，不要解释。"

TESTS = '''import unittest

from solution import parse_duration


class TestParseDuration(unittest.TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(parse_duration("1h30m"), 5400)

    def test_minutes_only(self):
        self.assertEqual(parse_duration("45m"), 2700)

    def test_seconds_only(self):
        self.assertEqual(parse_duration("30s"), 30)

    def test_days_hours_minutes_seconds(self):
        self.assertEqual(parse_duration("1d2h3m4s"), 93784)

    def test_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("半天")
'''

V1_BUGGY = '''```python
def parse_duration(text: str) -> int:
    """把 "1h30m" 这类时长文本转成秒数。"""
    return int(text[:-1]) * 60
```'''

V2_FIXED = '''```python
import re

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PATTERN = re.compile(r"(\\d+)([dhms])")


def parse_duration(text: str) -> int:
    """把 "1h30m" 这类时长文本转成秒数；无法解析时抛 ValueError。"""
    total = 0
    matched = 0
    for value, unit in _PATTERN.findall(text):
        total += int(value) * _UNIT_SECONDS[unit]
        matched += 1
    if matched == 0:
        raise ValueError(f"无法解析时长：{text!r}")
    if _PATTERN.sub("", text).strip():
        raise ValueError(f"包含无法识别的片段：{text!r}")
    return total
```'''


def _generate_prompt(task: str) -> str:
    return (
        f"任务：{task}\n\n"
        "要求：\n"
        '- 签名必须是 parse_duration(text: str) -> int\n'
        '- 支持 d/h/m/s 任意组合，例如 "1h30m"、"1d2h3m4s"、"45m"、"30s"\n'
        "- 无法解析的输入必须抛 ValueError\n"
        "只输出一个 python 代码块。"
    )


def _revise_prompt(code: str, critique: str, task: str) -> str:
    return (
        f"任务：{task}\n\n"
        f"当前实现：\n```python\n{code}\n```\n\n"
        f"{critique}\n\n"
        "请根据失败证据修正实现，仍然只输出一个 python 代码块。"
    )


def _rules() -> tuple[Rule, ...]:
    return (
        Rule(name="revise", pattern=r"(?s)测试结果：", reply=V2_FIXED),
        Rule(name="generate", pattern=r"(?s).*", reply=V1_BUGGY),
    )


_FALLBACK = "```python\nraise NotImplementedError\n```"


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    max_iterations: int = 3,
    keep_workdir: bool = False,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    llm = llm or build_llm(llm_kind, rules=_rules(), fallback=_FALLBACK, label="reflection")
    trace = trace if trace is not None else Trace()

    trace.add("step", f"任务：{task}", f"验收标准：{TESTS.count('def test_')} 个单测全部通过，最多反思 {max_iterations} 轮")

    code = extract_code(llm.ask(_generate_prompt(task), system=SYSTEM_PROMPT))
    trace.add("message", "首轮实现", code)

    history: list[dict[str, Any]] = []
    for iteration in range(1, max_iterations + 1):
        run_result = run_unit_tests(code, TESTS, keep=keep_workdir)
        trace.add(
            "critique",
            f"第 {iteration} 轮评审：通过 {run_result.passed}/{run_result.ran}（score={run_result.score():.2f}）",
            run_result.critique(),
        )
        history.append(
            {
                "iteration": iteration,
                "passed": run_result.passed,
                "ran": run_result.ran,
                "score": round(run_result.score(), 3),
                "ok": run_result.ok,
                "workdir": str(run_result.workdir) if run_result.workdir else None,
            }
        )
        if run_result.ok:
            trace.add("final", "单测全部通过，评审标准已满足，停止反思")
            return PatternResult(
                pattern="reflection",
                task=task,
                ok=True,
                output=code,
                steps=iteration,
                llm_calls=llm.calls,
                notes=[f"反思 {iteration - 1} 轮后达标；成本约为单次生成的 {iteration} 倍"],
                extra={"history": history, "code": code},
            )
        if iteration == max_iterations:
            break
        reply = llm.ask(_revise_prompt(code, run_result.critique(), task), system=SYSTEM_PROMPT)
        code = extract_code(reply)
        trace.add("message", f"第 {iteration} 轮改写后的实现", code)

    trace.add("error", f"达到最大反思轮数 {max_iterations}，单测仍未全部通过")
    return PatternResult(
        pattern="reflection",
        task=task,
        ok=False,
        output=code,
        steps=max_iterations,
        llm_calls=llm.calls,
        notes=["迭代上限兜底：没有上限的「自我完善」会无限烧钱"],
        extra={"history": history, "code": code},
    )
