"""七个模式的登记表与统一结果类型。

每个模式模块对外暴露同一个入口::

    run(task=None, *, llm=None, llm_kind="scripted", trace=None, **options) -> PatternResult

模块按需惰性导入，避免 ``import mm_agent.patterns`` 时就拉起全部示例。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLM
from ..trace import Trace


@dataclass(frozen=True)
class PatternInfo:
    """模式元数据：既用于 CLI 列表，也用于生成 README 对照表。"""

    key: str
    module: str
    name: str
    zh_name: str
    summary: str
    default_task: str
    when_to_use: str
    pitfall: str
    readiness: str


@dataclass
class PatternResult:
    """一次模式运行的统一产出。"""

    pattern: str
    task: str
    ok: bool
    output: str
    steps: int = 0
    llm_calls: int = 0
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def summary_line(self) -> str:
        status = "成功" if self.ok else "失败"
        return f"[{status}] {self.pattern}：步数 {self.steps}，模型调用 {self.llm_calls}，输出 {len(self.output)} 字"


PATTERNS: tuple[PatternInfo, ...] = (
    PatternInfo(
        key="tool-use",
        module="tool_use",
        name="Tool Use",
        zh_name="工具调用",
        summary="模型选择工具、给出参数、读取结果；事实来自工具而非参数记忆。",
        default_task="用计算器算出 (1234*56)/7，把结果保存到标题为 calc-demo 的笔记，然后汇报。",
        when_to_use="需要外部事实或副作用（查询、计算、下单、发信）时，几乎所有 Agent 的基础组件。",
        pitfall="模型会用错参数名、漏必填参数；工具静默失败会被模型当成「没有数据」进而编造。",
        readiness="生产可用",
    ),
    PatternInfo(
        key="react",
        module="react",
        name="ReAct",
        zh_name="推理-行动循环",
        summary="Thought → Action → Observation 循环，边走边看，直到能给出最终答复。",
        default_task="api-gateway 大量返回 502，定位原因并恢复服务。",
        when_to_use="路径无法预先确定、需要根据每步结果改变下一步的任务（排障、检索、数据探索）。",
        pitfall="重复调用同一工具导致死循环；thought/observation 堆积撑爆上下文；单步出错污染后续推理。",
        readiness="生产可用（需步数与重复动作护栏）",
    ),
    PatternInfo(
        key="reflection",
        module="reflection",
        name="Reflection",
        zh_name="自我反思",
        summary="生成 → 用明确标准评审 → 依据评审改写，直到达标或触顶。",
        default_task="实现 parse_duration(text)：把 '1h30m' 这类时长文本转成秒数，并通过随附单测。",
        when_to_use="首次产出质量不足的概率高、且错误可以被客观检验的场景（代码、报告、文案）。",
        pitfall="评审标准含糊时反复空转；评审本身出错会把正确结果改坏；成本近似翻倍。",
        readiness="有条件可用（依赖可判定的标准与迭代上限）",
    ),
    PatternInfo(
        key="planning",
        module="planning",
        name="Planning (Plan-and-Execute)",
        zh_name="先规划后执行",
        summary="先产出显式计划并校验，再逐步执行；步骤失败触发重规划，计划可落盘续跑。",
        default_task="产出 2026Q3 销售报告摘要。",
        when_to_use="任务结构清晰、步骤间有依赖、需要省 token 或需要断点续跑的长流程。",
        pitfall="计划本身错了后面全错；不校验计划会带着未知工具或坏依赖开跑；失败不重规划就卡死。",
        readiness="有条件可用（必须有计划校验与重规划）",
    ),
    PatternInfo(
        key="multi-agent",
        module="multi_agent",
        name="Multi-Agent Collaboration",
        zh_name="多智能体协作",
        summary="编排者按角色分派给研究员/撰稿/审稿，通过共享黑板传递状态并回流不合格产出。",
        default_task="写一份 200 字以内的决策简报：企业是否应该自建 Agent 平台，至少 2 条带编号来源的论据。",
        when_to_use="子任务需要不同提示词、不同工具或不同模型档位，单 Agent 的工具选择已明显退化时。",
        pitfall="角色间通信丢信息、协调成本高、多角色互相矛盾时难以定位；调试成本最高。",
        readiness="谨慎使用（失败面最大）",
    ),
    PatternInfo(
        key="sequential",
        module="sequential",
        name="Sequential Workflows",
        zh_name="顺序工作流",
        summary="步骤由开发者写死，LLM 调用像管道一样串联，步骤间用显式契约校验。",
        default_task="把主题「企业自建 Agent 平台的成本」加工成一篇结构化短博客。",
        when_to_use="流程稳定、可预测性与可审计性优先于自适应能力时；多数「Agent 产品」的真实形态。",
        pitfall="不会自适应：上游输出格式不符合下游期待时直接断链，必须在步骤间做断言与校验。",
        readiness="生产可用（最稳）",
    ),
    PatternInfo(
        key="human-in-the-loop",
        module="human_in_the_loop",
        name="Human-in-the-Loop",
        zh_name="人在回路",
        summary="按风险策略在关键动作前暂停，把上下文交给人决策，批准后继续、拒绝则终止且不产生副作用。",
        default_task="处理客户 C-1024 的 8600 元退款申请（订单已超过 30 天）。",
        when_to_use="涉及不可逆或对外的动作：退款、扣费、删除数据、对外发信、合规决策。",
        pitfall="无人审批的升级点会变成瓶颈；审批入口离开审批人日常工作流则响应极慢；超时无兜底会永久挂起。",
        readiness="生产可用（对外/涉钱场景的事实要求）",
    ),
)

_BY_KEY = {info.key: info for info in PATTERNS}


def get_pattern(key: str) -> PatternInfo:
    normalized = key.strip().lower()
    if normalized not in _BY_KEY:
        raise KeyError(f"未知模式 {key!r}；可选：{', '.join(_BY_KEY)}")
    return _BY_KEY[normalized]


def load_module(key: str):
    """按登记表惰性导入模式模块。"""

    return importlib.import_module(f".{get_pattern(key).module}", package=__name__)


def run_pattern(
    key: str,
    *,
    task: str | None = None,
    llm: LLM | None = None,
    llm_kind: str = "scripted",
    trace: Trace | None = None,
    **options: Any,
) -> PatternResult:
    """统一入口；``options`` 原样透传给模式模块（如 ``--approve``、``--resume``）。"""

    module = load_module(key)
    return module.run(task=task, llm=llm, llm_kind=llm_kind, trace=trace, **options)
