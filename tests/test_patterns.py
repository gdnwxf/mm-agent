"""七个模式的端到端单测。

测试全部使用离线假模型，因此不依赖网络与 API Key；断言的是模式行为本身：
护栏是否生效、失败路径是否可定位、副作用是否发生在审批之后、状态能否续跑。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from mm_agent.llm import LLM, Message
from mm_agent.patterns import PATTERNS, PatternResult, run_pattern
from mm_agent.patterns import human_in_the_loop, multi_agent, planning, sequential
from mm_agent.patterns.human_in_the_loop import AutoApprover, TimeoutApprover
from mm_agent.state import JsonStateStore
from mm_agent.trace import Trace


class _LoopLLM(LLM):
    """永远返回同一个动作，用于验证 ReAct 的重复动作护栏。"""

    def __init__(self) -> None:
        super().__init__("loop")

    def _generate(self, prompt: str, messages: Sequence[Message], *, stop: Sequence[str] | None) -> str:
        return 'Thought: 再看一眼状态。\nAction: service_status\nAction Input: {"service": "api-gateway"}'


def _run(key: str, **options: Any) -> PatternResult:
    return run_pattern(key, trace=Trace(), **options)


class ToolUseTests(unittest.TestCase):
    def test_recovers_from_schema_error(self) -> None:
        result = _run("tool-use")
        self.assertTrue(result.ok)
        self.assertIn("9872", result.output)
        actions = result.extra["actions"]
        self.assertFalse(actions[0]["ok"], "第一次调用应当因参数名错误被拦下")
        self.assertIn("缺少必填参数 expression", actions[0]["error"])
        self.assertTrue(actions[1]["ok"])
        self.assertIn("9872", result.extra["notes_store"]["calc-demo"])

    def test_step_guard_stops_runaway_loop(self) -> None:
        result = _run("tool-use", max_steps=2)
        self.assertFalse(result.ok)
        self.assertIn("护栏", result.notes[0])


class ReactTests(unittest.TestCase):
    def test_tool_sequence_and_notification(self) -> None:
        result = _run("react")
        self.assertTrue(result.ok)
        self.assertEqual(
            [item["tool"] for item in result.extra["actions"]],
            ["service_status", "fetch_logs", "restart_service", "notify"],
        )
        self.assertEqual(len(result.extra["notifications"]), 1)

    def test_repeat_action_guard_aborts(self) -> None:
        result = _run("react", llm=_LoopLLM(), repeat_limit=3)
        self.assertFalse(result.ok)
        self.assertIn("重复动作", result.notes[0])
        self.assertEqual(len(result.extra["actions"]), 2, "第 3 次相同动作应当在执行前被拦下")


class ReflectionTests(unittest.TestCase):
    def test_converges_after_one_revision(self) -> None:
        result = _run("reflection")
        self.assertTrue(result.ok)
        history = result.extra["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual((history[0]["passed"], history[0]["ran"]), (2, 5))
        self.assertTrue(history[-1]["ok"])
        self.assertIn("_UNIT_SECONDS", result.extra["code"])

    def test_iteration_cap_is_enforced(self) -> None:
        result = _run("reflection", max_iterations=1)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.extra["history"]), 1)
        self.assertIn("上限", result.notes[0])


class PlanningTests(unittest.TestCase):
    def test_validate_plan_reports_structural_problems(self) -> None:
        registry = planning.build_registry(planning.MockWarehouse())
        steps = [
            planning.PlanStep(id="s1", goal="a", tool="query_sales_db", args={}),
            planning.PlanStep(id="s1", goal="b", tool="不存在的工具", args={}),
            planning.PlanStep(id="s3", goal="c", tool="clean_data", args={"dataset": "x"}, depends_on=["s9"]),
        ]
        problems = planning.validate_plan(steps, registry)
        self.assertTrue(any("id 重复" in item for item in problems))
        self.assertTrue(any("未知工具" in item for item in problems))
        self.assertTrue(any("缺少必填参数 quarter" in item for item in problems))
        self.assertTrue(any("依赖不存在的步骤 s9" in item for item in problems))

    def test_completed_steps_are_valid_dependencies(self) -> None:
        registry = planning.build_registry(planning.MockWarehouse())
        steps = [planning.PlanStep(id="s2b", goal="扩充", tool="expand_dataset", args={"dataset": "${s2}", "extra_days": 30}, depends_on=["s2"])]
        self.assertEqual(planning.validate_plan(steps, registry, completed=["s2"]), [])
        self.assertTrue(planning.validate_plan(steps, registry))

    def test_replans_after_step_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planning.json"
            result = _run("planning", state_path=path)
            self.assertTrue(result.ok)
            self.assertIn("摘要", result.output)
            self.assertEqual(len(result.extra["rounds"]), 2, "应当经历一次重规划")
            self.assertIn("s2b", result.extra["results"], "重规划补的步骤应当被执行")
            self.assertTrue(path.exists())

            resumed = _run("planning", state_path=path, resume=True)
            self.assertTrue(resumed.ok)
            self.assertEqual(resumed.output, result.output)
            self.assertIn("断点续跑", resumed.notes[0])


class MultiAgentTests(unittest.TestCase):
    def test_review_loop_and_budget(self) -> None:
        result = _run("multi-agent")
        self.assertTrue(result.ok)
        rounds = result.extra["rounds"]
        self.assertEqual(len(rounds), 2, "第 1 稿应当被打回并重写")
        self.assertFalse(rounds[0]["passed"])
        self.assertTrue(rounds[1]["passed"])
        self.assertLessEqual(len(result.output), multi_agent.WORD_LIMIT)
        self.assertIn("[S1]", result.output)
        self.assertIn("[S2]", result.output)
        self.assertEqual(set(result.extra["budget"]), {"orchestrator", "researcher", "writer", "reviewer"})

    def test_first_draft_violates_the_rubric(self) -> None:
        self.assertGreater(len(multi_agent.DRAFT_V1), multi_agent.WORD_LIMIT)
        problems = multi_agent.rubric(multi_agent.DRAFT_V1, {"S1": "x"})
        self.assertTrue(any("超过上限" in item for item in problems))
        self.assertTrue(any("至少" in item for item in problems))

    def test_rubric_rejects_unknown_citation(self) -> None:
        problems = multi_agent.rubric("结论 [S1] 与 [S9]", {"S1": "x"})
        self.assertTrue(any("不存在的 [S9]" in item for item in problems))


class SequentialTests(unittest.TestCase):
    def test_pipeline_contracts_hold(self) -> None:
        result = _run("sequential")
        self.assertTrue(result.ok)
        self.assertTrue(result.output.startswith("# "))
        self.assertEqual(result.extra["steps"], ["关键词抽取", "生成提纲", "撰写草稿", "SEO 检查", "格式化输出"])

    def test_broken_outline_localises_failure(self) -> None:
        result = _run("sequential", broken_outline=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.extra["failed_step"], "生成提纲")
        self.assertEqual(result.extra["steps_done"], 1)
        self.assertIn("章节 1 个", result.extra["reason"])


class HumanInTheLoopTests(unittest.TestCase):
    def test_pauses_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hitl.json"
            result = _run("human-in-the-loop", state_path=path)
            self.assertFalse(result.ok)
            self.assertTrue(result.extra["waiting"])
            self.assertEqual(result.extra["gate"], "refund")
            self.assertEqual(result.extra["actions"], {}, "暂停时不得产生任何副作用")
            self.assertEqual(result.extra["approvals"], [])
            state = JsonStateStore(path).load()
            self.assertEqual(state["status"], "waiting_approval")
            self.assertEqual(state["stage"], "assess")

    def test_resume_with_approval_executes_both_gated_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hitl.json"
            _run("human-in-the-loop", state_path=path)
            result = _run(
                "human-in-the-loop",
                state_path=path,
                resume=True,
                approver=AutoApprover(approved=True, by="test-approver"),
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(result.extra["refunds"]), 1)
            self.assertEqual(len(result.extra["messages"]), 1)
            self.assertEqual(len(result.extra["approvals"]), 2)
            self.assertTrue(all(item["by"] == "test-approver" for item in result.extra["approvals"]))
            self.assertGreater(result.llm_calls, 0, "对外文案由模型起草，调用次数应当被记入")

    def test_rejection_blocks_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hitl.json"
            result = _run("human-in-the-loop", state_path=path, approver=AutoApprover(approved=False, by="test-approver"))
            self.assertFalse(result.ok)
            self.assertIn("未产生任何副作用", result.notes[0])
            self.assertEqual(result.extra["actions"], {})
            self.assertEqual(len(result.extra["approvals"]), 1)

    def test_timeout_fails_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hitl.json"
            result = _run("human-in-the-loop", state_path=path, approver=TimeoutApprover())
            self.assertFalse(result.ok)
            record = result.extra["approvals"][0]
            self.assertFalse(record["approved"])
            self.assertEqual(record["by"], "system")
            self.assertIn("fail-safe", record["reason"])

    def test_resume_after_rejection_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hitl.json"
            _run("human-in-the-loop", state_path=path, approver=AutoApprover(approved=False, by="test-approver"))
            result = _run("human-in-the-loop", state_path=path, resume=True, approver=AutoApprover(approved=True, by="test-approver"))
            self.assertFalse(result.ok)
            self.assertEqual(len(result.extra["approvals"]), 1)
            self.assertNotIn("refunds", result.extra, "被拒绝的申请续跑也不得退款")


class RegistryTests(unittest.TestCase):
    def test_seven_patterns_are_registered_and_runnable_offline(self) -> None:
        self.assertEqual(len(PATTERNS), 7)
        self.assertEqual(
            [info.key for info in PATTERNS],
            ["tool-use", "react", "reflection", "planning", "multi-agent", "sequential", "human-in-the-loop"],
        )


if __name__ == "__main__":
    unittest.main()
