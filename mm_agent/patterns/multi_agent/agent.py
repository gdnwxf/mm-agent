"""模式 5：Multi-Agent Collaboration（多智能体协作）。

编排者（orchestrator）先产出分工与验收标准，再把任务分派给角色化的智能体：
研究员 → 撰稿 → 审稿。中间产物放在共享黑板（blackboard）上，审稿不通过时把意见
回退给撰稿人重写，最多两轮。

三个设计要点：
- **角色隔离**：每个角色只拿到自己的 system 提示词与工具白名单（研究员有检索工具，
  撰稿人没有工具，审稿人只有机械校验结果）。
- **验收由代码兜底**：审稿人即使说「通过」，机械校验（字数、引用编号是否存在）不达标
  依然打回——多智能体里最危险的是多个角色互相确认错误的结论。
- **成本可见**：每个角色消耗的模型调用单独记账，多智能体是七种模式里最贵的。

注意：研究员内部跑的其实是一个两步 ReAct 循环，模式在实践中是叠加使用的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ...llm import LLM, Message, Rule, build_llm
from ...protocol import ProtocolError, extract_json, parse_step
from ...tools import ToolRegistry, tool
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "写一份 200 字以内的决策简报：企业是否应该自建 Agent 平台，至少 2 条带编号来源的论据。"

ORCHESTRATOR_SYSTEM = "你是一个任务编排者，只输出一个 JSON 对象，不要解释。"
RESEARCHER_SYSTEM = "你是一个研究员，先检索事实，再用带编号的来源汇总要点。"
WRITER_SYSTEM = "你是一个撰稿人，只输出正文，不加标题与解释，不做无来源的断言。"
REVIEWER_SYSTEM = "你是一个严格的审稿人，只依据给出的验收标准与机械校验结果给出结论。"

KNOWN_ROLES = ("researcher", "writer", "reviewer")
WORD_LIMIT = 200
MIN_CITATIONS = 2

RESEARCHER_FINDINGS = (
    "要点：\n"
    "[S1] 自建平台三年总拥有成本约为托管方案的 2.1 倍\n"
    "[S2] 78% 的受访企业选择从托管方案起步\n"
    "[S3] 自建需要模型网关、工具网关、权限审计、评测回归四套基础设施"
)

DRAFT_V1 = (
    "企业是否自建 Agent 平台，取决于三个现实约束。第一是成本：自建需要模型网关、工具网关、"
    "权限与审计、评测与回归四套基础设施，三年总拥有成本约为托管方案的 2.1 倍 [S1]。第二是人才："
    "需要同时懂业务编排与工程治理的团队，招聘与留存成本常被低估，而托管方案把这部分转化为服务费用。"
    "第三是合规：金融与医疗场景要求数据不出域，自建在这一点上确实有不可替代的优势。综合来看，"
    "多数企业的正确路径是先用托管方案验证业务价值，把编排层与工具层做成可迁移的抽象，"
    "等某个约束真正成为瓶颈时再考虑自建，而不是一开始就投入重资产。"
)

DRAFT_V2 = (
    "多数企业不必自建 Agent 平台：自建需要模型网关、工具网关、权限审计与评测四套设施，"
    "三年总拥有成本约为托管方案的 2.1 倍 [S1]。真正的自建理由只有一个——数据不出域的合规要求 [S2]。"
    "建议先用托管方案验证业务价值，把编排与工具层做成可迁移的抽象，等约束成为瓶颈后再自建。"
)


@dataclass
class Blackboard:
    """共享状态：多智能体之间只通过它交换信息，避免消息在角色间丢失。"""

    task: str
    brief: dict[str, Any] = field(default_factory=dict)
    findings: str = ""
    sources: dict[str, str] = field(default_factory=dict)
    draft: str = ""
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def register_sources(self) -> dict[str, str]:
        """从研究员产出里抽取 ``[S#] 论据`` 行，作为可被引用的来源清单。"""

        for index, text in re.findall(r"\[S(\d+)\]\s*(.+)", self.findings):
            self.sources[f"S{index}"] = text.strip()
        return self.sources


class MockSearch:
    """内存里的假检索：结果自带来源编号。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_web(self, query: str) -> str:
        self.queries.append(query)
        return (
            "检索结果：\n"
            "S1: 托管与自建三年 TCO 对比，自建约为 2.1 倍（2026 行业调研）\n"
            "S2: 78% 的受访企业选择从托管方案起步"
        )

    def kb_lookup(self, topic: str) -> str:
        self.queries.append(f"kb:{topic}")
        return "知识库：自建需要模型网关、工具网关、权限审计、评测回归四套基础设施（S3）"


def build_registry(search: MockSearch) -> ToolRegistry:
    @tool(
        name="search_web",
        description="检索外部资料，返回带编号来源的摘要。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索词"}},
            "required": ["query"],
        },
    )
    def search_web(query: str) -> str:
        return search.search_web(query)

    @tool(
        name="kb_lookup",
        description="查询内部知识库。",
        parameters={
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "主题"}},
            "required": ["topic"],
        },
    )
    def kb_lookup(topic: str) -> str:
        return search.kb_lookup(topic)

    return ToolRegistry([search_web, kb_lookup])


def rubric(draft: str, sources: dict[str, str]) -> list[str]:
    """机械校验：这几条不达标，审稿人说什么都不算通过。"""

    problems: list[str] = []
    body = draft.strip()
    if len(body) > WORD_LIMIT:
        problems.append(f"字数 {len(body)} 超过上限 {WORD_LIMIT}")
    cited = {f"S{index}" for index in re.findall(r"\[S(\d+)\]", body)}
    if len(cited) < MIN_CITATIONS:
        problems.append(f"带来源编号的论据只有 {len(cited)} 条，要求至少 {MIN_CITATIONS} 条")
    for key in sorted(cited):
        if key not in sources:
            problems.append(f"引用了来源清单里不存在的 [{key}]")
    return problems


def _brief_prompt(task: str) -> str:
    return (
        f"目标：{task}\n\n"
        "请输出 JSON 分工，格式：\n"
        '{"roles": [{"role": "researcher|writer|reviewer", "goal": "该角色要达成什么"}], "acceptance": "验收标准"}\n'
        "要求：三个角色都要出现，顺序为 researcher → writer → reviewer。"
    )


def _writer_prompt(task: str, findings: str, sources: dict[str, str], critique: str = "") -> str:
    source_lines = "\n".join(f"[{key}] {text}" for key, text in sorted(sources.items()))
    parts = [
        f"目标：{task}",
        f"可用来源（引用时必须使用这些编号）：\n{source_lines}",
        f"研究员要点：\n{findings}",
        f"硬性要求：正文不超过 {WORD_LIMIT} 字，至少引用 {MIN_CITATIONS} 条来源编号。",
    ]
    if critique:
        parts.append(f"上一轮审稿意见：\n{critique}\n请据此重写正文。")
    return "\n\n".join(parts)


def _reviewer_prompt(draft: str, problems: list[str]) -> str:
    checklist = "；".join(problems) if problems else "无"
    return f"验收标准：字数不超过 {WORD_LIMIT} 字；至少 {MIN_CITATIONS} 条带编号来源；引用必须存在于来源清单。\n机械校验结果：\n未通过项：{checklist}\n待审正文：\n{draft}"


BRIEF = json.dumps(
    {
        "roles": [
            {"role": "researcher", "goal": "检索并汇总至少 3 条带编号来源的事实"},
            {"role": "writer", "goal": f"写出不超过 {WORD_LIMIT} 字、至少 {MIN_CITATIONS} 条引用的简报"},
            {"role": "reviewer", "goal": "按字数与引用标准审稿，不通过则说明原因"},
        ],
        "acceptance": f"字数≤{WORD_LIMIT} 且至少 {MIN_CITATIONS} 条来源编号，引用必须存在",
    },
    ensure_ascii=False,
)


def _orchestrator_rules() -> tuple[Rule, ...]:
    return (Rule(name="brief", pattern=r"(?s)请输出 JSON 分工", reply=BRIEF),)


def _researcher_rules() -> tuple[Rule, ...]:
    return (
        Rule(
            name="summarize",
            pattern=r"(?s)Observation: .*S3",
            reply=f"Final Answer: {RESEARCHER_FINDINGS}",
        ),
        Rule(
            name="kb-lookup",
            pattern=r"(?s)Observation: .*S1",
            reply='Thought: 还缺基础设施层面的依据，查内部知识库。\nAction: kb_lookup\nAction Input: {"topic": "自建 Agent 平台所需基础设施"}',
        ),
        Rule(
            name="search",
            pattern=r"(?s)\A(?!.*Observation:)",
            reply='Thought: 先检索自建与托管的成本对比。\nAction: search_web\nAction Input: {"query": "自建 Agent 平台 三年 TCO 对比 托管"}',
        ),
    )


def _writer_rules() -> tuple[Rule, ...]:
    return (
        Rule(name="rewrite", pattern=r"(?s)上一轮审稿意见", reply=DRAFT_V2),
        Rule(name="draft", pattern=r"(?s)研究员要点", reply=DRAFT_V1),
    )


def _reviewer_rules() -> tuple[Rule, ...]:
    def verdict(_match: re.Match[str], prompt: str) -> str:
        found = re.search(r"未通过项：(.*)", prompt)
        items = found.group(1).strip() if found else "未知"
        if items == "无":
            return "通过：字数与引用均达标。"
        return f"不通过：{items}"

    return (Rule(name="verdict", pattern=r"(?s)未通过项：", reply=verdict),)


_FALLBACK = "无法生成回复。"


@dataclass
class _Budget:
    """按角色记账模型调用次数，多智能体的成本必须可见。"""

    calls: dict[str, int] = field(default_factory=dict)

    def spend(self, role: str, llm: LLM, before: int) -> None:
        self.calls[role] = self.calls.get(role, 0) + (llm.calls - before)


@dataclass
class _Role:
    name: str
    system: str
    rules: tuple[Rule, ...]

    def llm(self, kind: str) -> LLM:
        return build_llm(kind, rules=self.rules, fallback=_FALLBACK, label=self.name)


def _run_researcher(llm: LLM, registry: ToolRegistry, task: str, trace: Trace, max_steps: int = 4) -> tuple[str, list[dict[str, Any]]]:
    """研究员内部是一个小号 ReAct 循环：工具白名单只有检索类工具。"""

    messages = [
        Message("system", f"{RESEARCHER_SYSTEM}\n\n可用工具：\n{registry.describe(only=('search_web', 'kb_lookup'))}"),
        Message(
            "user",
            f"任务：{task}\n\n请先调用工具检索事实，再用 Final Answer 输出要点，每条要点以 [S编号] 开头。",
        ),
    ]
    actions: list[dict[str, Any]] = []
    for _step in range(max_steps):
        reply = llm.chat(messages)
        try:
            step = parse_step(reply)
        except ProtocolError as exc:
            messages.append(Message("assistant", reply))
            messages.append(Message("user", f"Observation: 输出格式错误：{exc}"))
            continue
        if step.is_final:
            return step.final_answer or "", actions
        result = registry.call(step.action or "", step.action_input)
        trace.add("action", f"[researcher] {step.action} {json.dumps(step.action_input, ensure_ascii=False)}")
        trace.add("observation", result.as_observation())
        actions.append({"tool": step.action, "ok": result.ok, "output": result.output})
        messages.append(Message("assistant", reply))
        messages.append(Message("user", f"Observation: {result.as_observation()}"))
    return "", actions


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    max_write_rounds: int = 2,
    roles: dict[str, LLM] | None = None,
    **_: Any,
) -> PatternResult:
    """运行多智能体协作。

    ``llm`` 只覆盖编排者；三个角色各有自己的提示词与规则，需要注入时用
    ``roles={"writer": ...}`` 按角色覆盖。
    """

    task = task or DEFAULT_TASK
    trace = trace if trace is not None else Trace()
    search = MockSearch()
    registry = build_registry(search)
    budget = _Budget()

    orchestrator = (roles or {}).get("orchestrator") or llm or build_llm(llm_kind, rules=_orchestrator_rules(), fallback=_FALLBACK, label="orchestrator")
    researcher = (roles or {}).get("researcher") or _Role("researcher", RESEARCHER_SYSTEM, _researcher_rules()).llm(llm_kind)
    writer = (roles or {}).get("writer") or _Role("writer", WRITER_SYSTEM, _writer_rules()).llm(llm_kind)
    reviewer = (roles or {}).get("reviewer") or _Role("reviewer", REVIEWER_SYSTEM, _reviewer_rules()).llm(llm_kind)

    trace.add("step", f"任务：{task}", f"角色：{' / '.join(KNOWN_ROLES)}；验收：字数≤{WORD_LIMIT}、至少 {MIN_CITATIONS} 条来源编号")

    # 1) 编排：产出分工与验收标准，并校验角色集合与顺序。
    before = orchestrator.calls
    reply = orchestrator.ask(_brief_prompt(task), system=ORCHESTRATOR_SYSTEM)
    budget.spend("orchestrator", orchestrator, before)
    try:
        brief = extract_json(reply)
        role_names = [str(item.get("role")) for item in brief.get("roles", [])]
    except (ProtocolError, AttributeError, TypeError) as exc:
        trace.add("error", f"分工解析失败：{exc}")
        return _abort(task, budget, [f"编排输出不可用：{exc}"])
    if role_names != list(KNOWN_ROLES):
        trace.add("error", f"分工校验失败：期望 {list(KNOWN_ROLES)}，实际 {role_names}")
        return _abort(task, budget, [f"分工校验失败：{role_names}"])
    trace.add("message", "[orchestrator] 分工与验收标准", json.dumps(brief, ensure_ascii=False, indent=2))

    board = Blackboard(task=task, brief=brief)

    # 2) 研究员：带工具的小号 ReAct 循环。
    before = researcher.calls
    board.findings, actions = _run_researcher(researcher, registry, task, trace)
    budget.spend("researcher", researcher, before)
    sources = board.register_sources()
    if not board.findings or not sources:
        trace.add("error", "研究员没有产出可引用的来源，任务终止")
        return _abort(task, budget, ["研究员产出为空或没有来源编号"])
    trace.add("message", f"[researcher] 来源清单（{len(sources)} 条）", "\n".join(f"[{key}] {text}" for key, text in sorted(sources.items())))

    # 3) 撰稿 → 审稿，不通过则回退重写。
    rounds: list[dict[str, Any]] = []
    for round_index in range(1, max_write_rounds + 1):
        critique = rounds[-1]["review"] if rounds and not rounds[-1]["passed"] else ""
        before = writer.calls
        board.draft = writer.ask(_writer_prompt(task, board.findings, sources, critique), system=WRITER_SYSTEM).strip()
        budget.spend("writer", writer, before)
        trace.add("message", f"[writer] 第 {round_index} 稿（{len(board.draft)} 字）", board.draft)

        problems = rubric(board.draft, sources)
        before = reviewer.calls
        review = reviewer.ask(_reviewer_prompt(board.draft, problems), system=REVIEWER_SYSTEM).strip()
        budget.spend("reviewer", reviewer, before)

        # 代码兜底：审稿人说通过但机械校验不达标，依然不通过。
        passed = not problems and "不通过" not in review
        if not problems and "不通过" in review:
            trace.add("error", "审稿人给出「不通过」但机械校验无问题，按保守策略打回")
        trace.add("message", f"[reviewer] 第 {round_index} 轮结论", review)
        rounds.append({"round": round_index, "passed": passed, "rubric": problems, "review": review, "length": len(board.draft)})
        if passed:
            trace.add("final", board.draft)
            return PatternResult(
                pattern="multi-agent",
                task=task,
                ok=True,
                output=board.draft,
                steps=len(rounds),
                llm_calls=sum(budget.calls.values()),
                notes=[
                    f"撰稿 {len(rounds)} 轮（含 {len(rounds) - 1} 次回退重写）；每条角色消息都是一次模型调用",
                    "模型调用分布：" + "、".join(f"{name} {count}" for name, count in sorted(budget.calls.items())),
                ],
                extra={"rounds": rounds, "sources": sources, "budget": budget.calls, "actions": actions},
            )

    trace.add("error", f"达到最大重写轮数 {max_write_rounds}，仍未通过验收")
    return PatternResult(
        pattern="multi-agent",
        task=task,
        ok=False,
        output=board.draft,
        steps=len(rounds),
        llm_calls=sum(budget.calls.values()),
        notes=[f"重写轮数上限兜底（{max_write_rounds}）", "多智能体不保证收敛，必须有轮数上限"],
        extra={"rounds": rounds, "sources": sources, "budget": budget.calls},
    )


def _abort(task: str, budget: _Budget, notes: list[str]) -> PatternResult:
    return PatternResult(
        pattern="multi-agent",
        task=task,
        ok=False,
        output="",
        steps=0,
        llm_calls=sum(budget.calls.values()),
        notes=notes,
        extra={"budget": budget.calls},
    )
