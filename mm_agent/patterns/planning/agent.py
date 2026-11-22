"""模式 4：Planning（Plan-and-Execute）。

核心：先用一次模型调用产出**显式计划**，校验通过后逐步执行；某步失败时带着失败
上下文**重新规划**，而不是原地硬撑。计划与已完成步骤落到 JSON，因此可以 ``--resume``
断点续跑。

示例任务：产出 2026Q3 销售报告摘要。

示例刻意覆盖两个生产坑：
- 计划校验：首版计划把参数名写错（``dataset_path``），在开跑前就被校验拦下；
- 失败重规划：分析步骤因样本量不足失败，重规划补一步「扩充数据集」后继续，已完成的
  步骤不会被重跑。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ...llm import LLM, Rule, build_llm
from ...protocol import ProtocolError, extract_json
from ...state import JsonStateStore
from ...tools import ToolRegistry, tool
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "产出 2026Q3 销售报告摘要。"
DEFAULT_STATE = Path("runs/planning-state.json")

PLANNER_SYSTEM = "你是一个任务规划器，只输出一个 JSON 对象，不要解释。"


class PlanError(ProtocolError):
    """计划不可用：结构不合法、引用了不存在的步骤输出等。"""


@dataclass(frozen=True)
class PlanStep:
    """计划中的一步：目标 + 工具 + 参数 + 依赖。"""

    id: str
    goal: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "PlanStep":
        if not isinstance(raw, dict):
            raise PlanError(f"计划步骤必须是 JSON 对象，实际为 {type(raw).__name__}")
        missing = [key for key in ("id", "goal", "tool") if not raw.get(key)]
        if missing:
            raise PlanError(f"计划步骤缺少字段：{', '.join(missing)}")
        args = raw.get("args") or {}
        depends_on = raw.get("depends_on") or []
        if not isinstance(args, dict):
            raise PlanError(f"步骤 {raw.get('id')} 的 args 必须是 JSON 对象")
        if not isinstance(depends_on, list):
            raise PlanError(f"步骤 {raw.get('id')} 的 depends_on 必须是数组")
        return cls(
            id=str(raw["id"]),
            goal=str(raw["goal"]),
            tool=str(raw["tool"]),
            args=args,
            depends_on=[str(item) for item in depends_on],
        )


def parse_plan(reply: str) -> list[PlanStep]:
    """从模型输出中解析计划；结构问题一律抛 :class:`PlanError`。"""

    data = extract_json(reply)
    raw_steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError('计划必须是 {"steps": [...]} 形式且至少包含一个步骤')
    return [PlanStep.from_dict(item) for item in raw_steps]


def validate_plan(steps: list[PlanStep], registry: ToolRegistry, completed: Iterable[str] = ()) -> list[str]:
    """开跑前的静态校验：id、工具、参数、依赖。

    这些都是「不校验就会在运行时才炸」的结构性错误，成本极低但收益很高。
    ``completed`` 是断点续跑时已完成的步骤：重规划可以依赖它们，因为它们的结果已经在
    执行上下文里。
    """

    problems: list[str] = []
    ids = [step.id for step in steps]
    for duplicate in sorted({item for item in ids if ids.count(item) > 1}):
        problems.append(f"步骤 id 重复：{duplicate}")
    known = set(ids) | set(completed)
    for step in steps:
        if step.tool not in registry:
            problems.append(f"步骤 {step.id} 使用未知工具 {step.tool}；可用工具：{', '.join(registry.names)}")
        else:
            item = registry.get(step.tool)
            properties = set(item.parameters.get("properties", {}))
            for key in sorted(set(step.args) - properties):
                problems.append(f"步骤 {step.id} 给 {step.tool} 传了未知参数 {key}")
            for key in item.parameters.get("required", []):
                if key not in step.args:
                    problems.append(f"步骤 {step.id} 调用 {step.tool} 缺少必填参数 {key}")
        for dependency in step.depends_on:
            if dependency not in known:
                problems.append(f"步骤 {step.id} 依赖不存在的步骤 {dependency}")
    return problems


class MockWarehouse:
    """内存里的假数仓：默认样本量不足以触发重规划。"""

    def __init__(self) -> None:
        self.rows = 128
        self.expanded = False


def build_registry(warehouse: MockWarehouse) -> ToolRegistry:
    @tool(
        name="query_sales_db",
        description="按季度拉取销售原始数据，返回数据集文件名与行数。",
        parameters={
            "type": "object",
            "properties": {"quarter": {"type": "string", "description": "季度，例如 2026Q3"}},
            "required": ["quarter"],
        },
    )
    def query_sales_db(quarter: str) -> str:
        return f"sales_{quarter}.csv rows={warehouse.rows}"

    @tool(
        name="expand_dataset",
        description="回补历史数据以扩充样本量。",
        parameters={
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "待扩充的数据集"},
                "extra_days": {"type": "integer", "description": "回补的天数"},
            },
            "required": ["dataset", "extra_days"],
        },
    )
    def expand_dataset(dataset: str, extra_days: int) -> str:
        warehouse.expanded = True
        warehouse.rows = 260
        base = dataset.split(" rows=")[0]
        return f"{base} rows={warehouse.rows}（含 {extra_days} 天历史回补）"

    @tool(
        name="clean_data",
        description="清洗并规范化数据集。",
        parameters={
            "type": "object",
            "properties": {"dataset": {"type": "string", "description": "待清洗的数据集"}},
            "required": ["dataset"],
        },
    )
    def clean_data(dataset: str) -> str:
        return f"clean:{dataset}"

    @tool(
        name="analyze_trend",
        description="对数据集做趋势分析；样本量低于 200 行会失败。",
        parameters={
            "type": "object",
            "properties": {"dataset": {"type": "string", "description": "已清洗的数据集"}},
            "required": ["dataset"],
        },
    )
    def analyze_trend(dataset: str) -> str:
        if "rows=260" not in dataset:
            raise ValueError("样本量不足：128 行低于阈值 200，无法给出显著趋势，请先扩充数据集")
        return "trend: 2026Q3 环比 +12.4%（260 行样本，显著）"

    @tool(
        name="write_summary",
        description="把分析结论写成报告摘要。",
        parameters={
            "type": "object",
            "properties": {"findings": {"type": "string", "description": "分析结论"}},
            "required": ["findings"],
        },
    )
    def write_summary(findings: str) -> str:
        return f"2026Q3 销售摘要：{findings}；口径：已清洗数据集，样本 260 行。"

    @tool(
        name="validate_report",
        description="校验报告是否含摘要、趋势与口径说明。",
        parameters={
            "type": "object",
            "properties": {"report": {"type": "string", "description": "待校验的报告正文"}},
            "required": ["report"],
        },
    )
    def validate_report(report: str) -> str:
        if "摘要" not in report:
            raise ValueError("报告缺少摘要段")
        return "报告校验通过：含摘要、趋势与口径说明"

    return ToolRegistry([query_sales_db, expand_dataset, clean_data, analyze_trend, write_summary, validate_report])


def _plan(steps: list[dict[str, Any]]) -> str:
    return json.dumps({"steps": steps}, ensure_ascii=False)


PLAN_A_WRONG_ARG = _plan(
    [
        {"id": "s1", "goal": "拉取 2026Q3 原始销售数据", "tool": "query_sales_db", "args": {"quarter": "2026Q3"}},
        {"id": "s2", "goal": "清洗数据", "tool": "clean_data", "args": {"dataset_path": "${s1}"}, "depends_on": ["s1"]},
        {"id": "s3", "goal": "分析季度趋势", "tool": "analyze_trend", "args": {"dataset": "${s2}"}, "depends_on": ["s2"]},
        {"id": "s4", "goal": "撰写报告摘要", "tool": "write_summary", "args": {"findings": "${s3}"}, "depends_on": ["s3"]},
        {"id": "s5", "goal": "校验报告", "tool": "validate_report", "args": {"report": "${s4}"}, "depends_on": ["s4"]},
    ]
)

PLAN_A_FIXED = _plan(
    [
        {"id": "s1", "goal": "拉取 2026Q3 原始销售数据", "tool": "query_sales_db", "args": {"quarter": "2026Q3"}},
        {"id": "s2", "goal": "清洗数据", "tool": "clean_data", "args": {"dataset": "${s1}"}, "depends_on": ["s1"]},
        {"id": "s3", "goal": "分析季度趋势", "tool": "analyze_trend", "args": {"dataset": "${s2}"}, "depends_on": ["s2"]},
        {"id": "s4", "goal": "撰写报告摘要", "tool": "write_summary", "args": {"findings": "${s3}"}, "depends_on": ["s3"]},
        {"id": "s5", "goal": "校验报告", "tool": "validate_report", "args": {"report": "${s4}"}, "depends_on": ["s4"]},
    ]
)

PLAN_B_AFTER_FAILURE = _plan(
    [
        {"id": "s2b", "goal": "回补历史数据把样本量提到 260 行", "tool": "expand_dataset", "args": {"dataset": "${s2}", "extra_days": 30}, "depends_on": ["s2"]},
        {"id": "s3", "goal": "重新分析季度趋势", "tool": "analyze_trend", "args": {"dataset": "${s2b}"}, "depends_on": ["s2b"]},
        {"id": "s4", "goal": "撰写报告摘要", "tool": "write_summary", "args": {"findings": "${s3}"}, "depends_on": ["s3"]},
        {"id": "s5", "goal": "校验报告", "tool": "validate_report", "args": {"report": "${s4}"}, "depends_on": ["s4"]},
    ]
)


def _rules() -> tuple[Rule, ...]:
    # 顺序即优先级：重规划提示词里同时含「样本量不足」与「计划校验失败」，必须先匹配重规划，
    # 否则会被「修正计划」规则抢走，导致执行到一半又退回旧计划。
    return (
        Rule(name="replan", pattern=r"样本量不足", reply=PLAN_B_AFTER_FAILURE),
        Rule(name="fix-invalid-plan", pattern=r"计划校验失败", reply=PLAN_A_FIXED),
        Rule(name="plan", pattern=r"销售报告", reply=PLAN_A_WRONG_ARG),
    )


_FALLBACK = '{"steps": []}'


def _planner_prompt(task: str, registry: ToolRegistry, feedback: str = "") -> str:
    parts = [
        f"目标：{task}",
        f"可用工具：\n{registry.describe()}",
        "请输出 JSON 计划，格式：",
        '{"steps": [{"id": "s1", "goal": "这一步要达成什么", "tool": "工具名", "args": {"参数": "值"}, "depends_on": []}]}',
        "要求：按执行顺序排列；args 里可用 ${步骤id} 引用前序步骤的输出；一步只用一个工具。",
    ]
    if feedback:
        parts.append(f"上一轮计划失败信息：\n{feedback}")
    return "\n\n".join(parts)


_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def _resolve(value: Any, results: dict[str, str]) -> Any:
    """把计划参数里的 ``${步骤id}`` 换成已产出的实际结果。"""

    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in results:
                raise PlanError(f"引用了尚未成功执行的步骤输出 ${{{key}}}")
            return str(results[key])

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {key: _resolve(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, results) for item in value]
    return value


def _execute(plan: list[PlanStep], registry: ToolRegistry, results: dict[str, str], trace: Trace) -> tuple[str | None, str]:
    """执行计划；返回 (失败步骤 id 或 None, 失败原因)。已完成的步骤直接跳过。"""

    for step in plan:
        if step.id in results:
            trace.add("state", f"跳过已完成步骤 {step.id}（断点续跑）")
            continue
        try:
            args = _resolve(step.args, results)
        except PlanError as exc:
            trace.add("error", f"步骤 {step.id} 参数解析失败：{exc}")
            return step.id, str(exc)
        result = registry.call(step.tool, args)
        trace.add("action", f"{step.id} {step.tool} {json.dumps(args, ensure_ascii=False)}", step.goal)
        trace.add("observation", result.as_observation())
        if not result.ok:
            return step.id, result.error or "未知错误"
        results[step.id] = result.output
    return None, ""


def _final_output(plan: list[PlanStep], results: dict[str, str]) -> str:
    """取交付物正文：优先写报告那一步，否则退化为最后一个有输出的步骤。"""

    for step in plan:
        if step.tool == "write_summary" and step.id in results:
            return results[step.id]
    last = plan[-1]
    return results.get(last.id, "")


def _make_plan(
    planner: LLM,
    task: str,
    registry: ToolRegistry,
    feedback: str,
    trace: Trace,
    completed: Iterable[str] = (),
) -> tuple[list[PlanStep], list[str]]:
    """产出计划；最多尝试两次（第二次带着校验错误让规划器修正）。"""

    prompt = _planner_prompt(task, registry, feedback)
    problems: list[str] = ["计划在两次尝试内都没有通过校验"]
    for _attempt in range(2):
        reply = planner.ask(prompt, system=PLANNER_SYSTEM)
        try:
            plan = parse_plan(reply)
        except ProtocolError as exc:
            problems = [f"计划解析失败：{exc}"]
        else:
            problems = validate_plan(plan, registry, completed)
            if not problems:
                trace.add("plan", f"计划就绪（{len(plan)} 步）", "\n".join(f"{step.id}. {step.goal} → {step.tool}" for step in plan))
                return plan, []
        trace.add("error", "计划校验失败：" + "；".join(problems))
        prompt = prompt + "\n\n计划校验失败：" + "；".join(problems) + "\n请修正后只输出 JSON 计划。"
    return [], problems


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    state_path: str | Path | None = None,
    resume: bool = False,
    max_rounds: int = 3,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    store = JsonStateStore(state_path or DEFAULT_STATE)
    warehouse = MockWarehouse()
    registry = build_registry(warehouse)
    planner = llm or build_llm(llm_kind, rules=_rules(), fallback=_FALLBACK, label="planner")
    trace = trace if trace is not None else Trace()

    state: dict[str, Any] = {"task": task, "results": {}, "rounds": [], "status": "new"}
    if resume and store.exists():
        state = store.load()
        task = str(state.get("task") or task)
        state.setdefault("results", {})
        state.setdefault("rounds", [])
        trace.add("state", f"从 {store.path} 恢复（status={state.get('status')}）", f"已完成步骤：{', '.join(state['results']) or '无'}")
        if state.get("status") == "done":
            trace.add("state", "上次已成功完成，直接返回落盘结果")
            return PatternResult(
                pattern="planning",
                task=task,
                ok=True,
                output=str(state.get("output", "")),
                steps=len(state["results"]),
                llm_calls=planner.calls,
                notes=["断点续跑：命中已完成状态，未再调用执行路径"],
                extra={"rounds": len(state["rounds"]), "state_path": str(store.path), "results": state["results"]},
            )
    elif resume:
        trace.add("state", f"没有找到状态文件 {store.path}，按全新任务执行")

    results: dict[str, str] = state["results"]
    state["results"] = results
    rounds: list[dict[str, Any]] = state["rounds"]
    feedback = ""
    last_output = ""

    for round_index in range(1, max_rounds + 1):
        plan, problems = _make_plan(planner, task, registry, feedback, trace, completed=results)
        if problems:
            state.update({"status": "plan_invalid"})
            store.save(state)
            return PatternResult(
                pattern="planning",
                task=task,
                ok=False,
                output="",
                steps=len(results),
                llm_calls=planner.calls,
                notes=["计划校验未通过，拒绝带病执行"],
                extra={"problems": problems, "state_path": str(store.path)},
            )
        rounds.append({"round": round_index, "plan": [asdict(step) for step in plan]})
        state.update({"status": "executing", "plan": [asdict(step) for step in plan]})
        store.save(state)

        failed_id, reason = _execute(plan, registry, results, trace)
        state["results"] = results
        store.save(state)
        if failed_id is None:
            last_output = _final_output(plan, results)
            state.update({"status": "done", "output": last_output})
            store.save(state)
            trace.add("final", last_output)
            return PatternResult(
                pattern="planning",
                task=task,
                ok=True,
                output=last_output,
                steps=len(results),
                llm_calls=planner.calls,
                notes=[
                    f"共 {round_index} 轮计划（含 {round_index - 1} 次重规划），执行 {len(results)} 个步骤",
                    f"状态已落盘：{store.path}，可用 --resume 续跑",
                ],
                extra={"rounds": rounds, "state_path": str(store.path), "results": results},
            )
        feedback = f"步骤 {failed_id} 执行失败，错误：{reason}"
        trace.add("plan", f"第 {round_index} 轮失败，触发重规划", feedback)
        if round_index == max_rounds:
            break

    state.update({"status": "failed"})
    store.save(state)
    trace.add("error", f"达到最大重规划轮数 {max_rounds}，任务未完成")
    return PatternResult(
        pattern="planning",
        task=task,
        ok=False,
        output="",
        steps=len(results),
        llm_calls=planner.calls,
        notes=[f"重规划轮数上限兜底（{max_rounds}）"],
        extra={"rounds": rounds, "state_path": str(store.path), "results": results},
    )
