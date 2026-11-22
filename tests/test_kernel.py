"""内核单测：假模型、文本协议、工具校验、状态存储、子进程测试运行器。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mm_agent.llm import LLMError, Message, Rule, build_llm, render_messages
from mm_agent.protocol import ProtocolError, extract_code, extract_json, parse_step
from mm_agent.pyrun import run_unit_tests
from mm_agent.state import JsonStateStore, StateNotFound
from mm_agent.tools import ToolError, ToolRegistry, tool


def _registry() -> ToolRegistry:
    @tool(
        name="echo",
        description="回显文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
            "required": ["text"],
        },
    )
    def echo(text: str, times: int = 1) -> str:
        return text * times

    @tool(
        name="boom",
        description="总是抛异常",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def boom() -> str:
        raise RuntimeError("内部故障")

    return ToolRegistry([echo, boom])


class LLMTests(unittest.TestCase):
    def test_rules_are_first_match_wins_and_calls_counted(self) -> None:
        llm = build_llm(
            "scripted",
            rules=(
                Rule(name="specific", pattern=r"继续", reply="第二个"),
                Rule(name="catch-all", pattern=r".*", reply="第一个"),
            ),
        )
        self.assertEqual(llm.ask("请继续"), "第二个")
        self.assertEqual(llm.ask("无关输入"), "第一个")
        self.assertEqual(llm.calls, 2)
        self.assertEqual([item["call"] for item in llm.history], [1, 2])

    def test_callable_rule_can_read_prompt(self) -> None:
        def reply(match, prompt: str) -> str:
            return f"命中 {match.group(1)}，提示词长度 {len(prompt) > 0}"

        llm = build_llm("scripted", rules=(Rule(name="dynamic", pattern=r"编号\s*(\d+)", reply=reply),))
        self.assertTrue(llm.ask("编号 7").endswith("提示词长度 True"))

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(LLMError):
            build_llm("不存在的模型")

    def test_render_messages_keeps_role_markers(self) -> None:
        text = render_messages([Message("system", "规则"), Message("user", "任务")])
        self.assertIn("[system]", text)
        self.assertIn("[user]", text)

    def test_openai_kind_requires_key(self) -> None:
        import os

        saved = {key: os.environ.pop(key, None) for key in ("MM_AGENT_API_KEY", "OPENAI_API_KEY")}
        try:
            with self.assertRaises(LLMError):
                build_llm("openai")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


class ProtocolTests(unittest.TestCase):
    def test_parse_action_step(self) -> None:
        step = parse_step('Thought: 先查状态\nAction: service_status\nAction Input: {"service": "api-gateway"}')
        self.assertEqual(step.thought, "先查状态")
        self.assertEqual(step.action, "service_status")
        self.assertEqual(step.action_input, {"service": "api-gateway"})
        self.assertFalse(step.is_final)

    def test_parse_final_answer(self) -> None:
        step = parse_step("Thought: 可以收尾\nFinal Answer: 已恢复")
        self.assertTrue(step.is_final)
        self.assertEqual(step.final_answer, "已恢复")

    def test_action_input_not_swallowing_observation(self) -> None:
        step = parse_step('Action: echo\nAction Input: {"text": "hi"}\nObservation: hi')
        self.assertEqual(step.action_input, {"text": "hi"})

    def test_bare_action_input_falls_back_to_input_key(self) -> None:
        step = parse_step("Action: echo\nAction Input: hello")
        self.assertEqual(step.action_input, {"input": "hello"})

    def test_invalid_output_raises(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_step("我觉得应该先看看日志。")

    def test_extract_code_and_json(self) -> None:
        self.assertEqual(extract_code("说明\n```python\nx = 1\n```\n结束"), "x = 1")
        self.assertEqual(extract_json('前文 {"a": [1, 2]} 后文'), {"a": [1, 2]})
        with self.assertRaises(ProtocolError):
            extract_json("没有 JSON")


class ToolTests(unittest.TestCase):
    def test_missing_required_and_unknown_argument(self) -> None:
        result = _registry().call("echo", {"expr": "hi"})
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数 text", result.error or "")
        self.assertIn("未知参数 expr", result.error or "")
        self.assertTrue((result.as_observation()).startswith("ERROR: "))

    def test_type_mismatch(self) -> None:
        result = _registry().call("echo", {"text": 1})
        self.assertFalse(result.ok)
        self.assertIn("期望 string", result.error or "")

    def test_bool_is_not_integer(self) -> None:
        result = _registry().call("echo", {"text": "x", "times": True})
        self.assertFalse(result.ok)
        self.assertIn("实际 boolean", result.error or "")

    def test_unknown_tool_lists_available(self) -> None:
        result = _registry().call("nope", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error or "")
        self.assertIn("echo", result.error or "")

    def test_handler_exception_becomes_readable_error(self) -> None:
        result = _registry().call("boom", {})
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error or "")

    def test_optional_argument_default_is_used(self) -> None:
        result = _registry().call("echo", {"text": "ab", "times": 2})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "abab")

    def test_duplicate_registration_raises(self) -> None:
        registry = _registry()
        with self.assertRaises(ToolError):
            registry.register(registry.get("echo"))

    def test_openai_schema_shape(self) -> None:
        schema = _registry().openai_schema(only=("echo",))
        self.assertEqual(schema[0]["type"], "function")
        self.assertEqual(schema[0]["function"]["name"], "echo")


class StateTests(unittest.TestCase):
    def test_roundtrip_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStateStore(Path(tmp) / "nested" / "state.json")
            with self.assertRaises(StateNotFound):
                store.load()
            store.save({"stage": "collect", "results": {"s1": "ok"}})
            self.assertTrue(store.exists())
            self.assertEqual(store.load()["results"], {"s1": "ok"})
            store.clear()
            self.assertFalse(store.exists())


class PyRunTests(unittest.TestCase):
    TESTS = "import unittest\nfrom solution import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"

    def test_passing_solution(self) -> None:
        result = run_unit_tests("def add(a, b):\n    return a + b\n", self.TESTS)
        self.assertTrue(result.ok)
        self.assertEqual((result.ran, result.passed, result.failed), (1, 1, 0))
        self.assertEqual(result.score(), 1.0)

    def test_failing_solution_reports_counts(self) -> None:
        result = run_unit_tests("def add(a, b):\n    return a - b\n", self.TESTS)
        self.assertFalse(result.ok)
        self.assertEqual((result.ran, result.passed, result.failed), (1, 0, 1))
        self.assertIn("测试结果", result.critique())

    def test_timeout_is_reported(self) -> None:
        result = run_unit_tests("while True:\n    pass\n", self.TESTS, timeout=1.0)
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)


if __name__ == "__main__":
    unittest.main()
