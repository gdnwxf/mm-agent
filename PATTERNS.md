# 七种模式详解

模式定义与「何时用 / 主要坑」来自 [S1][S2][S3]（见 [SOURCES.md](SOURCES.md)）；
每个模式的示例任务、注入的失败路径与实测数字是本仓库自写、实跑所得。

## 与 Anthropic 工作流分类的对应关系

Anthropic 在 [S4] 里区分了 **workflows**（LLM 与工具由预定义代码路径编排）与 **agents**
（LLM 自行决定过程与工具使用），并给出 prompt chaining / routing / parallelization /
orchestrator-workers / evaluator-optimizer / autonomous agent 六种形态。对应关系如下
（对应判断由本仓库给出，不是原文结论）：

| 本仓库模式 | Anthropic 对应 [S4] | 差异说明 |
| --- | --- | --- |
| Tool Use | augmented LLM（基础构件）+ 工具提示词工程（Appendix 2） | [S4] 把工具视为基础构件而非独立模式 |
| ReAct | Agents：`LLMs using tools based on environmental feedback in a loop` | [S4] 强调停止条件（最大迭代次数），本示例实现了两条护栏 |
| Reflection | evaluator-optimizer | [S4] 的版本是「一个生成 + 一个评估」的循环，本示例用单测结果当评估 |
| Planning | prompt chaining + gate 的规划化版本 | [S4] 没有独立的「先规划后执行」条目 |
| Multi-Agent | orchestrator-workers | [S4] 强调子任务由编排者动态决定 |
| Sequential Workflows | prompt chaining | [S4] 明确建议在中间步骤加程序化检查（gate），本示例用契约校验实现 |
| Human-in-the-Loop | Agents 段落中的 `pause for human feedback at checkpoints` | [S4] 未单列成模式 |

## 1. Tool Use（工具调用）

- **核心**：模型选工具 → 给参数 → 读结果。事实来自工具的 Observation，而不是模型记忆。
- **何时用**：需要外部事实或副作用（查询、计算、下单、发信）；几乎所有 Agent 的基础组件。
- **主要坑** [S1]：模型会用错参数名、漏必填参数；工具**静默失败**会被模型当成「没有数据」，
  进而编造答案。
- **示例任务**：算出 `(1234*56)/7`，写进标题 `calc-demo` 的笔记，再汇报。
- **看点**：第 1 次调用故意用错参数名 `expr`。`ToolRegistry.call` 返回
  `缺少必填参数 expression；未知参数 expr`，模型据此改正后成功。
- **代码位置**：工具定义与 Schema [tool_use/agent.py:75](mm_agent/patterns/tool_use/agent.py#L75)；
  校验与结构化错误 [tools.py:136](mm_agent/tools.py#L136)。
- **实测**：步数 4、模型调用 4，其中工具调用 3 次、失败 1 次（失败即被纠正）。

## 2. ReAct（Reason + Act）

- **核心**：`Thought → Action → Observation` 循环，边走边看，直到给出 `Final Answer`。
- **何时用**：路径无法预先确定、下一步依赖上一步真实结果的任务（排障、检索、数据探索）。
- **主要坑** [S1][S2]：重复调用同一工具的死循环；thought/observation 堆积撑爆上下文；
  单步错误污染后续推理。
- **示例任务**：api-gateway 大量 502，定位原因并恢复。
- **看点**：两条护栏——最大步数（默认 8）与重复动作检测（同工具同参数第 3 次即终止）。
  单测里用「永远返回同一动作」的假模型验证护栏真的会终止。
- **代码位置**：循环与护栏 [react/agent.py:207](mm_agent/patterns/react/agent.py#L207)、
  重复动作判定 [react/agent.py:250](mm_agent/patterns/react/agent.py#L250)。
- **实测**：步数 5（`service_status → fetch_logs → restart_service → notify`），模型调用 5 次。

## 3. Reflection（自我反思）

- **核心**：`Generate → Critique → Revise`，直到满足标准或触顶。
- **何时用**：首次产出质量不足的概率高，且错误可被客观检验（代码、报告、文案）。
- **主要坑** [S1]：评审标准含糊时反复空转；评审本身出错会把正确结果改坏；成本近似翻倍。
- **示例任务**：实现 `parse_duration(text)`，把 `"1h30m"` 转成 5400 秒，并通过 5 个单测。
- **看点**：批评者不是模型的随口点评，而是**在子进程里真实执行单测**的结果；候选代码死循环
  也不会拖住 Agent 本体（超时即失败）。
- **代码位置**：反思循环 [reflection/agent.py:109](mm_agent/patterns/reflection/agent.py#L109)；
  子进程执行 [pyrun.py:47](mm_agent/pyrun.py#L47)。
- **实测**：第 1 版通过 2/5 → 1 轮改写后 5/5；步数 2、模型调用 2。

## 4. Planning（Plan-and-Execute）

- **核心**：先产出显式计划并校验，再逐步执行；步骤失败时带着失败上下文**重新规划**。
- **何时用**：任务结构清晰、步骤间有依赖、需要省 token 或需要断点续跑的长流程。
- **主要坑** [S1][S2]：计划本身错了后面全错；不校验计划会带着未知工具或坏依赖开跑；
  失败后不重规划就卡死。
- **示例任务**：产出 2026Q3 销售报告摘要。
- **看点**：两处注入——首版计划把参数名写成 `dataset_path`（被 `validate_plan` 在开跑前拦下）；
  分析步骤因样本量不足失败（触发重规划补一步扩充数据集）。已完成的步骤不会被重跑，
  状态落盘后 `--resume` 可直接返回结果。
- **代码位置**：计划校验 [planning/agent.py:82](mm_agent/patterns/planning/agent.py#L82)；
  工具与假数仓 [planning/agent.py:120](mm_agent/patterns/planning/agent.py#L120)。
- **实测**：3 次规划调用（含 1 次校验修正、1 次重规划）、执行 6 个步骤、1 次步骤失败。

## 5. Multi-Agent Collaboration（多智能体协作）

- **核心**：编排者按角色分派，角色间通过共享状态交换产物；不合格产出回流重做。
- **何时用**：子任务需要不同提示词、不同工具或不同模型档位，单 Agent 的工具选择已明显退化时。
- **主要坑** [S1]：角色间通信丢信息；协调成本高；多角色互相确认错误结论；调试成本最高。
- **示例任务**：写一份 200 字以内、至少 2 条带编号来源的决策简报。
- **看点**：① 角色隔离（研究员有检索工具，撰稿人没有，审稿人只看机械校验结果）；
  ② 审稿人即使说「通过」，机械校验（字数、引用编号是否存在）不达标依然打回；
  ③ 按角色记账模型调用次数。
- **代码位置**：机械校验 [multi_agent/agent.py:130](mm_agent/patterns/multi_agent/agent.py#L130)；
  研究员的小号 ReAct 循环 [multi_agent/agent.py:251](mm_agent/patterns/multi_agent/agent.py#L251)。
- **实测**：第 1 稿 258 字 / 1 条引用被打回，第 2 稿 142 字 / 2 条引用通过；
  模型调用 8 次（编排者 1、研究员 3、撰稿 2、审稿 2）。

## 6. Sequential Workflows（顺序工作流）

- **核心**：步骤由开发者写死，像管道一样串联；每步职责单一、可单独测试。
- **何时用**：流程稳定、可预测性与可审计性优先于自适应能力时；多数「Agent 产品」的真实形态 [S2]。
- **主要坑** [S1]：不具自适应性，上游输出格式一变下游就炸；必须在步骤之间做校验。
- **示例任务**：把主题「企业自建 Agent 平台的成本」加工成结构化短博客。
- **看点**：步骤间用 dataclass 契约 + `validate()` 断言，且每个契约自带下游需要的上下文；
  `--bad-input` 注入只有 1 个章节的提纲，管道在第 2 步精确中断（`failed_step=生成提纲`）。
- **代码位置**：管道装配 [sequential/agent.py:227](mm_agent/patterns/sequential/agent.py#L227)；
  契约校验与失败定位 [sequential/agent.py:149](mm_agent/patterns/sequential/agent.py#L149)。
- **实测**：5 步全部完成，SEO 得分 1.00；模型调用 3 次（另两步是确定性代码）。

## 7. Human-in-the-Loop（人在回路）

- **核心**：在不可逆或对外的动作前暂停，把上下文与后果交给人决策；拒绝则终止且无副作用。
- **何时用**：涉钱、删除数据、对外发信、合规决策等做错代价高于一次人工确认的场景。
- **主要坑** [S1]：无人负责的审批点会变成瓶颈；审批入口离开审批人日常工作流则响应极慢；
  超时无兜底会永久挂起。
- **示例任务**：处理客户 C-1024 的 8600 元退款申请（订单已超 30 天）。
- **看点**：① 两个闸门（退款、对外消息），都默认拒绝（fail-safe）；
  ② 暂停点落盘，人工审批后在**另一个进程**里 `--resume` 继续；
  ③ 拒绝后状态落盘，再续跑也不会重试退款；④ 审批结论、审批人、理由全部留痕。
- **代码位置**：闸门与审批通道 [human_in_the_loop/approval.py:14](mm_agent/patterns/human_in_the_loop/approval.py#L14)；
  退款政策判定 [human_in_the_loop/refund.py:129](mm_agent/patterns/human_in_the_loop/refund.py#L129)；
  闸门处理 [human_in_the_loop/agent.py:240](mm_agent/patterns/human_in_the_loop/agent.py#L240)。
- **实测**：一次暂停时零副作用；`--resume --approve` 后退款与站内信各执行一次，2 次审批留痕；
  `--timeout-approver` 时由 `system` 按 fail-safe 拒绝。

## 选型顺序

[S2] 的判断是「先用最简模式，跑不通再升级」，本仓库的复杂度阶梯与之一致：
Sequential（写死步骤）→ Tool Use（接外部事实）→ ReAct（路径不定）→ Planning（结构清晰但长）
→ Reflection（质量不达标）→ Multi-Agent（单 Agent 已到能力上限）→ Human-in-the-Loop
（任何不可逆或对外动作的安全前提，与上面几条正交）。

## 架构图（ASCII）

每个图都按仓库里的真实实现绘制（依据代码见各小节），宽度不对齐时由生成脚本自检拦截。

### 全景：一个内核 + 七个模式目录

```text
一个内核（mm_agent/*.py）+ 七个模式目录（mm_agent/patterns/<模式>/）

                    ┌──────────────────────────────────────────────┐
                    │  llm.py     protocol.py   tools.py           │
                    │  trace.py   state.py      pyrun.py  cli.py   │
                    └───────────────────────┬──────────────────────┘
                                            │ 七个模式共用
  ┌──────────┬──────────┬──────────┬────────┴───┬──────────┬──────────┐
  ▼          ▼          ▼          ▼            ▼          ▼          ▼
 1 Tool     2 ReAct   3 Reflect  4 Planning  5 Multi-   6 Sequen   7 HITL
   Use                 ion                    Agent      tial
```

### 对应关系：本仓库七种模式 ↔ Agent 架构图分类

```text
Agent 架构图分类（[S4]）                                     本仓库对应

├── workflows（LLM 与工具由预定义代码路径编排）
│   ├── prompt chaining ──────────────────────────────▶ 6 Sequential
│   ├── routing ──────────────────────────────────────▶ 未实现（不在七种清单内）
│   ├── parallelization ──────────────────────────────▶ 未实现（不在七种清单内）
│   ├── orchestrator-workers ─────────────────────────▶ 5 Multi-Agent
│   └── evaluator-optimizer ──────────────────────────▶ 3 Reflection
└── agents（模型自行决定过程与工具使用）
    ├── augmented LLM（基础构件）────────────────────▶ 1 Tool Use
    ├── tool-use loop（环境反馈驱动的循环）──────────▶ 2 ReAct
    ├── plan → operate ──────────────────────────────▶ 4 Planning
    └── pause for human feedback at checkpoints ─────▶ 7 Human-in-the-Loop
```

### 1 Tool Use（工具调用）

依据代码：`mm_agent/patterns/tool_use/agent.py（工具定义与循环）`

```text
任务：用计算器算出 (1234*56)/7，写入笔记 calc-demo，再汇报
  │
  ▼
┌─ 模型 LLM.chat ──────────────────────────────────────────────────┐
│ 第 1 轮：Action: calculator，参数误用 expr                       │
│ → 结构化错误「Schema 要求 expression」→ 第 2 轮改正              │
│ 读到 Observation 后继续，直到输出 Final Answer                   │
└──────────────────────────────────────────────────────────────────┘
  │ Action（工具名 + JSON 参数）
  ▼
┌─ ToolRegistry.call —— 唯一的外部副作用入口 ──────────────────────┐
│ 1) 工具是否存在   2) JSON Schema 参数校验                        │
│ 3) 执行 handler    4) 校验失败 / 执行异常 → 结构化错误           │
│ 成功与失败都返回 ToolResult，绝不静默失败                        │
└──────────────────────────────────────────────────────────────────┘
  │ Observation（成功结果，或模型可读的错误）
  └─▶ 回到模型：expr 非法 → 改成 expression → 9872 → save_note

工具：calculator / save_note / list_notes / read_note；步数护栏 6
```

### 2 ReAct（推理-行动循环）

依据代码：`mm_agent/patterns/react/agent.py（循环与两条护栏）`

```text
任务：api-gateway 大量 502，定位原因并恢复服务
  │
  ▼
┌─ 循环体：一次迭代 = 一次模型调用 ────────────────────────────────┐
│ Thought ──▶ Action + Action Input ──▶ Observation ──┐            │
│   ▲                                                 │            │
│   └─────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────┘
  │ 本示例实际轨迹（5 步 / 模型调用 5 次）
  ▼
┌─ Thought → Action 序列 ──────────────────────────────────────────┐
│ 1 service_status{service=api-gateway} → status=degraded cpu=98%  │
│ 2 fetch_logs{service, lines=50}       → 连接池耗尽 / OOMKilled   │
│ 3 restart_service{service}            → status=healthy           │
│ 4 notify{channel, text}               → 已向 oncall 发送通知     │
│ 5 Final Answer：根因 + 已执行动作 + 后续建议                     │
└──────────────────────────────────────────────────────────────────┘
  │ 护栏（两条，缺一不可）
  ▼
┌─ 护栏 ───────────────────────────────────────────────────────────┐
│ ① 最大步数 8：超过即终止，不许无限循环                           │
│ ② 重复动作：同工具 + 同参数出现 3 次 → 判定死循环并终止          │
└──────────────────────────────────────────────────────────────────┘
```

### 3 Reflection（自我反思）

依据代码：`mm_agent/patterns/reflection/agent.py + mm_agent/pyrun.py`

```text
任务：实现 parse_duration(text)，并通过随附 5 个单测
  │
  ▼
┌─ ① Generate（LLM） ──────────────────────────────────────────────┐
│ 输出 python 代码块 → 候选实现（第 1 版只过 2/5）                 │
└──────────────────────────────────────────────────────────────────┘
  │ 候选代码
  ▼
┌─ ② Critique（确定性，不是模型随口点评） ─────────────────────────┐
│ pyrun.run_unit_tests：写临时目录 → 子进程跑 unittest             │
│ 10s 超时即判失败（候选代码死循环也拖不住 Agent）                 │
│ 产出：通过 2/5 + 失败堆栈，作为反思的反馈证据                    │
└──────────────────────────────────────────────────────────────────┘
  │ 未通过 → 把失败证据拼进提示词
  ▼
┌─ ③ Revise（LLM，携带批判意见） ──────────────────────────────────┐
│ 改写实现 → 再跑单测 → 5/5 通过，停止                             │
└──────────────────────────────────────────────────────────────────┘
  │
  └─▶ 迭代上限 3 轮兜底；每轮成本约等于一次完整生成
```

### 4 Planning（Plan-and-Execute）

依据代码：`mm_agent/patterns/planning/agent.py（校验 / 执行 / 重规划）`

```text
任务：产出 2026Q3 销售报告摘要
  │
  ▼
┌─ ① 规划器（LLM）输出 JSON 计划 ──────────────────────────────────┐
│ {"steps": [{id, goal, tool, args, depends_on}, ...]}             │
│ args 支持 ${步骤id} 引用前序输出；产出最多尝试 2 次              │
└──────────────────────────────────────────────────────────────────┘
  │ 计划
  ▼
┌─ ② validate_plan（开跑前静态校验） ──────────────────────────────┐
│ id 重复 / 未知工具 / 未知参数 / 缺必填参数 / 依赖不存在          │
│ 断点续跑时，已完成步骤可作为合法依赖（completed 参数）           │
│ 首版计划把 dataset_path 写错 → 在此被拦下并重规划                │
└──────────────────────────────────────────────────────────────────┘
  │ 通过
  ▼
┌─ ③ 逐步执行（跳过已完成步骤） ───────────────────────────────────┐
│ registry.call(step.tool, 解析后的 args) → results[step.id]       │
│ s3 analyze_trend 失败：样本量 128 行低于阈值 200                 │
└──────────────────────────────────────────────────────────────────┘
  │ 失败原因（不是直接退出）
  ▼
┌─ ④ 重规划（最多 3 轮） ──────────────────────────────────────────┐
│ 带失败上下文重新规划 → 补 s2b expand_dataset 把样本提到 260      │
│ s1、s2 已在 results 中 → 不重跑；状态落盘 planning-state.json    │
└──────────────────────────────────────────────────────────────────┘
  │
  └─▶ 全部成功 → 报告摘要；--resume 可跨进程续跑
```

### 5 Multi-Agent Collaboration（多智能体协作）

依据代码：`mm_agent/patterns/multi_agent/agent.py`

```text
任务：200 字以内的决策简报，至少 2 条带编号来源的论据
  │
  ▼
┌─ 编排者 orchestrator（LLM） ─────────────────────────────────────┐
│ 输出分工 JSON：roles[research → write → review] + acceptance     │
│ 代码校验角色集合与顺序，不合法即终止（不让编排幻觉扩散）         │
└──────────────────────────────────────────────────────────────────┘
  │ 分派
  ▼
┌─ ① researcher（工具白名单：search_web / kb_lookup） ─────────────┐
│ 内部是一个 2 步 ReAct 循环：检索 → 知识库 → Final Answer         │
│ 产出 findings，并抽出 [S1][S2][S3] 来源清单                      │
└──────────────────────────────────────────────────────────────────┘
  │ findings + sources
  ▼
┌─ 共享黑板 Blackboard（角色间唯一的交换介质） ────────────────────┐
│ task / brief / findings / sources / draft / reviews              │
└──────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ ② writer（无工具） → ③ reviewer（只看机械校验结果） ────────────┐
│ writer 第 1 稿 258 字、只引用 1 条 → 机械校验不达标              │
│ rubric：字数 ≤200、≥2 条来源编号、引用必须存在于来源清单         │
│ reviewer 说「通过」但 rubric 不达标时，代码兜底打回              │
│ 带审稿意见回退重写（≤2 轮）→ 第 2 稿 142 字、2 条引用 → 通过     │
└──────────────────────────────────────────────────────────────────┘
  │
  └─▶ 最终简报；模型调用按角色记账（1 / 3 / 2 / 2 = 8 次）
```

### 6 Sequential Workflows（顺序工作流）

依据代码：`mm_agent/patterns/sequential/agent.py（管道与契约）`

```text
任务：把主题「企业自建 Agent 平台的成本」加工成结构化短博客
  │
  ▼
┌─ 固定五步管道（步骤由开发者写死，无规划、无重试） ───────────────┐
│ 1 关键词抽取 → KeywordBrief   ┐                                  │
│ 2 生成提纲   → Outline        │ 每步之间：类型检查 + validate()  │
│ 3 撰写草稿   → Draft          │ 失败即整条中断，并报出步骤名     │
│ 4 SEO 检查   → SeoReport      │ 第 4、5 步不是 LLM               │
│ 5 格式化输出 → FinalPost      ┘                                  │
└──────────────────────────────────────────────────────────────────┘
  │ 正常路径：管道走完，实测 SEO 得分 1.00、模型调用 3 次
  │
  └─▶ --bad-input 注入坏提纲（只有 1 个章节）时：
┌─ 契约中断（可预测性的代价） ─────────────────────────────────────┐
│ 步骤「生成提纲」输出不符合契约：章节 1 个，要求 3~6 个           │
│ 管道在第 2/5 步中断，steps_done=1，坏数据不会流向第 3 步         │
└──────────────────────────────────────────────────────────────────┘
```

### 7 Human-in-the-Loop（人在回路）

依据代码：`mm_agent/patterns/human_in_the_loop/{agent,approval,refund}.py`

```text
任务：处理客户 C-1024 的 8600 元退款申请（订单已超 30 天）
  │
  ▼
┌─ 阶段机（每个阶段结束都把状态写进 hitl-state.json） ─────────────┐
│ new → collect → assess → gate-refund → refund                    │
│     → draft → gate-notify → notify → done                        │
└──────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ collect / assess（代码判定，可审计） ───────────────────────────┐
│ get_order / get_customer → 金额 8600、下单 47 天、VIP、退款 0 次 │
│ 政策：金额 ≥5000 或超 30 天 → risk=high；涉钱动作必须人工审批    │
└──────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ gate-refund —— 人工闸门（default_decision=False） ──────────────┐
│ 上下文：金额、判定依据、不可撤销、客户可见后果                   │
│ 无人响应 → fail-safe 默认拒绝；拒绝 → 终止且零副作用             │
└──────────────────────────────────────────────────────────────────┘
  │ 批准
  ▼
┌─ refund（不可逆副作用）→ draft（模型起草对外文案） ──────────────┐
│ payment_refund → 流水号 RF-1001                                  │
└──────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ gate-notify —— 第二个闸门（对外沟通，同样默认拒绝） ────────────┐
│ 批准 → send_customer_message；两次审批的结论/审批人/理由全部留痕 │
└──────────────────────────────────────────────────────────────────┘
  │
  └─▶ done；--resume 从前一阶段继续，被拒绝的申请不会重试
```

### 选型阶梯

```text
复杂度阶梯（[S2]：先用最简模式，跑不通再升级）

  6 Sequential ──▶ 1 Tool Use ──▶ 2 ReAct ──▶ 4 Planning
    写死步骤        接外部事实      路径不定      结构清但长

  3 Reflection（质量不达标时叠加） ──▶ 5 Multi-Agent（单 Agent 到顶）

  7 Human-in-the-Loop 与上面各条正交：不可逆 / 对外动作前的安全闸门
```
