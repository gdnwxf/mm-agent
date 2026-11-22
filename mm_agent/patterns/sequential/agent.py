"""模式 6：Sequential Workflows（顺序工作流）。

核心：步骤由开发者写死，像管道一样串联；每步职责单一、提示词短、可单独测试。
它没有「规划」能力，换来的是最高的可控性与可预测性——被宣传成 Agent 的产品里，
很大一部分本质就是精心设计的顺序工作流。

示例任务：把主题「企业自建 Agent 平台的成本」加工成一篇结构化短博客。

顺序工作流最大的坑是不具自适应性：上游输出格式一变，下游就炸。因此示例在每步之间
用 dataclass 契约 + ``validate()`` 做断言，并且每个契约自带下游需要的上下文，
失败时精确指出是哪一步的输出不合格，而不是等到最后一步报一个莫名其妙的错。
``broken_outline=True`` 会注入一次「提纲只有 1 个章节」的坏输出，演示这个失败路径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from ...llm import LLM, Rule, build_llm
from ...protocol import ProtocolError, extract_json
from ...trace import Trace
from .. import PatternResult

DEFAULT_TASK = "把主题「企业自建 Agent 平台的成本」加工成一篇结构化短博客。"

STEP_KEYWORDS = "【步骤】关键词抽取"
STEP_OUTLINE = "【步骤】生成提纲"
STEP_OUTLINE_BROKEN = "【步骤】生成提纲（异常注入）"
STEP_DRAFT = "【步骤】撰写草稿"


class _Contract:
    """步骤间契约的公共接口：能自述是否符合约定。"""

    def validate(self) -> list[str]:
        return []

    def describe(self) -> str:
        return ""


@dataclass(frozen=True)
class KeywordBrief(_Contract):
    topic: str
    audience: str
    keywords: tuple[str, ...]

    def validate(self) -> list[str]:
        problems = []
        if not self.topic.strip():
            problems.append("topic 为空")
        if not 3 <= len(self.keywords) <= 6:
            problems.append(f"关键词 {len(self.keywords)} 个，要求 3~6 个")
        return problems

    def describe(self) -> str:
        return f"{len(self.keywords)} 个关键词：{'、'.join(self.keywords)}"


@dataclass(frozen=True)
class Outline(_Contract):
    """提纲；携带关键词，下游不必回头找上一步的输出。"""

    title: str
    sections: tuple[str, ...]
    keywords: tuple[str, ...]

    def validate(self) -> list[str]:
        problems = []
        if not self.title.strip():
            problems.append("title 为空")
        if not 3 <= len(self.sections) <= 6:
            problems.append(f"章节 {len(self.sections)} 个，要求 3~6 个")
        if not self.keywords:
            problems.append("关键词为空，说明上游契约没有正确传递")
        return problems

    def describe(self) -> str:
        return f"《{self.title}》{len(self.sections)} 个章节"


@dataclass(frozen=True)
class Draft(_Contract):
    title: str
    paragraphs: tuple[str, ...]
    keywords: tuple[str, ...]

    @property
    def body(self) -> str:
        return "\n\n".join(self.paragraphs)

    def validate(self) -> list[str]:
        problems = []
        if len(self.paragraphs) < 3:
            problems.append(f"段落 {len(self.paragraphs)} 个，少于下限 3 个")
        if len(self.body) < 120:
            problems.append(f"正文 {len(self.body)} 字，少于下限 120 字")
        return problems

    def describe(self) -> str:
        return f"{len(self.paragraphs)} 段、{len(self.body)} 字"


@dataclass(frozen=True)
class SeoReport(_Contract):
    """SEO 检查结果；携带草稿，排版步骤因此不需要额外的共享状态。"""

    score: float
    problems: tuple[str, ...]
    draft: Draft

    def validate(self) -> list[str]:
        return []

    def describe(self) -> str:
        return f"SEO 得分 {self.score:.2f}，问题 {len(self.problems)} 项"


@dataclass(frozen=True)
class FinalPost(_Contract):
    title: str
    markdown: str
    seo_score: float

    def validate(self) -> list[str]:
        problems = []
        if not self.markdown.startswith("# "):
            problems.append("markdown 必须以一级标题开头")
        if self.title not in self.markdown:
            problems.append("正文缺少标题")
        return problems

    def describe(self) -> str:
        return f"{len(self.markdown)} 字的成稿"


@dataclass
class Step:
    """一个管道步骤：把上一步的输出转成下一步的输入。"""

    name: str
    output_type: type
    run: Callable[[Any], Any]


def run_pipeline(steps: list[Step], initial: Any, trace: Trace) -> tuple[Any, str | None, str, int]:
    """顺序执行；返回 (最终产出, 失败步骤名, 失败原因, 已完成步数)。

    这里刻意不做重试或降级：顺序工作流的价值是「要么按预期完成，要么立刻暴露问题」，
    偷偷容错反而会让坏数据流向出口。
    """

    current = initial
    for index, step in enumerate(steps, start=1):
        trace.add("step", f"{index}/{len(steps)} {step.name}", f"输入：{type(current).__name__}")
        try:
            output = step.run(current)
        except ProtocolError as exc:
            # 模型没按 JSON 契约输出：同样是「某一步的输出不合格」，位置必须精确。
            trace.add("error", f"步骤 {step.name} 的输出无法解析：{exc}")
            return None, step.name, f"输出无法解析：{exc}", index - 1
        if not isinstance(output, step.output_type):
            return None, step.name, f"期望 {step.output_type.__name__}，实际 {type(output).__name__}", index - 1
        problems = output.validate()
        if problems:
            trace.add("error", f"步骤 {step.name} 的输出不符合契约：{'；'.join(problems)}")
            return None, step.name, f"输出不符合契约：{'；'.join(problems)}", index - 1
        trace.add("contract", f"{step.name} → {type(output).__name__}", f"契约校验通过（{output.describe()}）")
        current = output
    return current, None, "", len(steps)


def _rules() -> tuple[Rule, ...]:
    """三个 LLM 步骤各有一个提示词标记，假模型据此返回对应结构化输出。"""

    def keywords(_match: re.Match[str], prompt: str) -> str:
        found = re.search(r"主题：(.+)", prompt)
        topic = found.group(1).strip() if found else "未命名主题"
        return json.dumps(
            {"topic": topic, "audience": "技术负责人", "keywords": ["Agent 平台", "自建成本", "三年 TCO", "托管方案"]},
            ensure_ascii=False,
        )

    def outline(broken: bool):
        def reply(_match: re.Match[str], prompt: str) -> str:
            found = re.search(r"主题：(.+)", prompt)
            topic = found.group(1).strip() if found else "未命名主题"
            sections = ["为什么现在要算这笔账", "三年 TCO 拆解", "自建与托管的分界点", "决策建议"]
            if broken:
                sections = sections[:1]
            return json.dumps({"title": f"{topic}：先算清三年 TCO", "sections": sections}, ensure_ascii=False)

        return reply

    def draft(_match: re.Match[str], prompt: str) -> str:
        title_match = re.search(r"标题：(.+)", prompt)
        section_match = re.search(r"章节：(.+)", prompt)
        keywords_match = re.search(r"关键词：(.+)", prompt)
        title = title_match.group(1).strip() if title_match else "未命名标题"
        sections = [item.strip() for item in (section_match.group(1).split("｜") if section_match else []) if item.strip()]
        items = [item.strip() for item in (keywords_match.group(1).split("、") if keywords_match else []) if item.strip()]
        lead = items[0] if items else "主题"
        second = items[1] if len(items) > 1 else lead
        paragraphs = [
            f"{name}。围绕「{lead}」，这里给出结论与依据：把可量化的部分（人数、周期、费用）写成数字，"
            f"把不可量化的部分标注为假设，避免只谈感受不谈账。"
            for name in sections
        ]
        if paragraphs:
            paragraphs[-1] = f"{sections[-1]}。综合「{lead}」与「{second}」，建议先验证再投入，并保留回退路径。"
        return json.dumps({"title": title, "paragraphs": paragraphs}, ensure_ascii=False)

    return (
        Rule(name="broken-outline", pattern=re.escape(STEP_OUTLINE_BROKEN), reply=outline(True)),
        Rule(name="keywords", pattern=re.escape(STEP_KEYWORDS), reply=keywords),
        Rule(name="outline", pattern=re.escape(STEP_OUTLINE), reply=outline(False)),
        Rule(name="draft", pattern=re.escape(STEP_DRAFT), reply=draft),
    )


_FALLBACK = "{}"


def build_pipeline(llm: LLM, *, broken_outline: bool = False) -> list[Step]:
    """装配五个步骤：三步 LLM 加工 + 两步确定性检查/排版。"""

    def step_keywords(topic_seed: Any) -> KeywordBrief:
        topic = str(topic_seed).strip()
        reply = llm.ask(
            f'{STEP_KEYWORDS}\n主题：{topic}\n请输出 JSON：{{"topic":..., "audience":..., "keywords":[...]}}'
        )
        data = extract_json(reply)
        return KeywordBrief(
            topic=str(data.get("topic", topic)),
            audience=str(data.get("audience", "未指定")),
            keywords=tuple(str(item) for item in data.get("keywords", [])),
        )

    def step_outline(brief: KeywordBrief) -> Outline:
        marker = STEP_OUTLINE_BROKEN if broken_outline else STEP_OUTLINE
        reply = llm.ask(
            f'{marker}\n主题：{brief.topic}\n关键词：{"、".join(brief.keywords)}\n'
            '请输出 JSON：{"title":..., "sections":[...]}'
        )
        data = extract_json(reply)
        return Outline(
            title=str(data.get("title", "")),
            sections=tuple(str(item) for item in data.get("sections", [])),
            keywords=brief.keywords,
        )

    def step_draft(outline: Outline) -> Draft:
        reply = llm.ask(
            f"{STEP_DRAFT}\n标题：{outline.title}\n章节：{'｜'.join(outline.sections)}\n"
            f'关键词：{"、".join(outline.keywords)}\n请输出 JSON：{{"title":..., "paragraphs":[...]}}'
        )
        data = extract_json(reply)
        return Draft(
            title=str(data.get("title", outline.title)),
            paragraphs=tuple(str(item) for item in data.get("paragraphs", [])),
            keywords=outline.keywords,
        )

    def step_seo(draft: Draft) -> SeoReport:
        """确定性 SEO 检查：管道里的非 LLM 步骤同样是第一等公民。"""

        problems: list[str] = []
        body = draft.body
        if draft.keywords:
            if draft.keywords[0] not in draft.title:
                problems.append(f"标题缺少主关键词「{draft.keywords[0]}」")
            missing = [item for item in draft.keywords[:2] if item not in body]
            if missing:
                problems.append(f"正文缺少关键词：{'、'.join(missing)}")
        if len(body) < 200:
            problems.append(f"正文 {len(body)} 字，建议不少于 200 字")
        checks = 3
        return SeoReport(score=round((checks - len(problems)) / checks, 2), problems=tuple(problems), draft=draft)

    def step_format(report: SeoReport) -> FinalPost:
        markdown = "\n\n".join([f"# {report.draft.title}", *report.draft.paragraphs])
        if report.problems:
            markdown += "\n\n<!-- SEO 待改进：" + "；".join(report.problems) + " -->"
        return FinalPost(title=report.draft.title, markdown=markdown, seo_score=report.score)

    return [
        Step("关键词抽取", KeywordBrief, step_keywords),
        Step("生成提纲", Outline, step_outline),
        Step("撰写草稿", Draft, step_draft),
        Step("SEO 检查", SeoReport, step_seo),
        Step("格式化输出", FinalPost, step_format),
    ]


def run(
    task: str | None = None,
    *,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    broken_outline: bool = False,
    **_: Any,
) -> PatternResult:
    task = task or DEFAULT_TASK
    topic = task
    if "「" in task and "」" in task:
        topic = task.split("「", 1)[1].split("」", 1)[0]
    llm = llm or build_llm(llm_kind, rules=_rules(), fallback=_FALLBACK, label="sequential")
    trace = trace if trace is not None else Trace()

    trace.add("step", f"任务：{task}", "流程固定为：关键词 → 提纲 → 草稿 → SEO → 排版（无规划、无重试）")
    steps = build_pipeline(llm, broken_outline=broken_outline)
    output, failed_step, reason, completed = run_pipeline(steps, topic, trace)

    if failed_step is not None:
        trace.add("error", f"管道在第 {completed + 1} 步「{failed_step}」中断：{reason}")
        return PatternResult(
            pattern="sequential",
            task=task,
            ok=False,
            output="",
            steps=completed,
            llm_calls=llm.calls,
            notes=[
                f"流水线不具自适应性：第 {completed + 1}/{len(steps)} 步「{failed_step}」输出不合格即整条中断，不会自行修复",
                "价值在于失败点精确：报错直接指向出问题的步骤，而不是最后一步的连带错误",
            ],
            extra={"failed_step": failed_step, "reason": reason, "steps_done": completed},
        )

    post: FinalPost = output
    trace.add("final", post.markdown)
    return PatternResult(
        pattern="sequential",
        task=task,
        ok=True,
        output=post.markdown,
        steps=len(steps),
        llm_calls=llm.calls,
        notes=[f"五步管道全部完成，SEO 得分 {post.seo_score:.2f}；每步可单独测试与替换"],
        extra={"seo_score": post.seo_score, "steps": [step.name for step in steps]},
    )
