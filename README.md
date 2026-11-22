# mm-agent

Agent 七种设计模式的**离线可运行**示例集。每个模式一个模块，共享同一套小内核
（LLM 抽象 / 文本协议 / 工具注册 / 执行轨迹 / 状态存储），因此读任意一个模块看到的都是完整
的模式循环，而不是框架胶水代码。

- 默认使用**确定性假模型**（`ScriptedLLM`）：不联网、不需要 API Key、输出可复现、可断言；
- 换真实模型只需 `--llm openai`（OpenAI 兼容接口，需 `MM_AGENT_API_KEY` / `OPENAI_API_KEY`）；
- 纯标准库实现，Python ≥ 3.10；
- 43 个单测覆盖内核与七个模式的正常、边界与失败路径。

七种模式的清单与每个模式的「何时用 / 主要坑 / 生产可用度」取自公开资料 [S1][S2][S3]；
与 Anthropic 工作流分类的对应关系见 [PATTERNS.md](PATTERNS.md)；逐条出处与未验证项见
[SOURCES.md](SOURCES.md)。示例代码、任务场景与所有实测数字均为本仓库自写、实跑所得。

## 快速开始

```bash
cd mm-agent

python3 -m mm_agent list                 # 列出七个模式
python3 -m mm_agent info react           # 查看某个模式的适用场景与坑
python3 -m mm_agent run react            # 运行单个模式（打印完整 Thought/Action/Observation 轨迹）
python3 -m mm_agent run all              # 七个模式依次跑一遍
python3 -m mm_agent run sequential --bad-input        # 演示契约中断的失败路径
python3 -m mm_agent run planning --resume             # 断点续跑
python3 -m mm_agent run human-in-the-loop             # 暂停等人工审批
python3 -m mm_agent run human-in-the-loop --resume --approve

python3 -m unittest discover -s tests -t .            # 单测
```

每个模式模块也可以单独运行，例如 `python3 -m mm_agent.patterns.react`。
需要 `mm-agent` 命令时再 `uv pip install -e .`（可选，示例本身不需要安装）。

## 七种模式

| # | 模式 | 模块 | 示例任务 | 生产可用度 [S1] |
| --- | --- | --- | --- | --- |
| 1 | Tool Use（工具调用） | [tool_use/agent.py](mm_agent/patterns/tool_use/agent.py) | 算 (1234*56)/7 并把结果写进笔记 | 生产可用 |
| 2 | ReAct（推理-行动循环） | [react/agent.py](mm_agent/patterns/react/agent.py) | 定位 api-gateway 的 502 并恢复服务 | 生产可用（需护栏） |
| 3 | Reflection（自我反思） | [reflection/agent.py](mm_agent/patterns/reflection/agent.py) | 实现 `parse_duration` 并通过单测 | 有条件可用 |
| 4 | Planning（Plan-and-Execute） | [planning/agent.py](mm_agent/patterns/planning/agent.py) | 产出 2026Q3 销售报告摘要 | 有条件可用 |
| 5 | Multi-Agent（多智能体协作） | [multi_agent/agent.py](mm_agent/patterns/multi_agent/agent.py) | 写 200 字以内的决策简报 | 谨慎使用 |
| 6 | Sequential Workflows（顺序工作流） | [sequential/agent.py](mm_agent/patterns/sequential/agent.py) | 主题 → 提纲 → 草稿 → SEO → 排版 | 生产可用（最稳） |
| 7 | Human-in-the-Loop（人在回路） | [human_in_the_loop/agent.py](mm_agent/patterns/human_in_the_loop/agent.py) | 8600 元退款申请的审批与执行 | 生产可用（涉钱/对外场景的事实要求） |

每个模式对应的 ASCII 架构图（含与 Agent 架构图分类的对应关系）见
[PATTERNS.md](PATTERNS.md) 的「架构图（ASCII）」一节。

每个模式都刻意注入了一个真实世界的失败路径，而不是只演示顺利路径：

| 模式 | 注入的失败 | 期望观察到的行为 |
| --- | --- | --- |
| Tool Use | 模型第一次调用用错参数名（`expr`） | Schema 校验拦下 → 结构化错误 → 模型自我修正 |
| ReAct | 单测里用「永远返回同一动作」的假模型 | 重复动作护栏在第 3 次前终止，`ok=False` |
| Reflection | 首版实现只过 2/5 个单测 | 用真实单测结果做批评 → 改写到 5/5；迭代上限兜底 |
| Planning | 首版计划参数名写错；分析步骤样本量不足 | 计划校验拦下并重规划；补一步扩充数据集后继续，已完成步骤不重跑 |
| Multi-Agent | 第 1 稿 258 字、只引用 1 条来源 | 机械校验 + 审稿人打回 → 重写为 142 字、2 条引用 |
| Sequential | `--bad-input` 注入只有 1 个章节的提纲 | 管道在第 2 步精确中断，不把坏数据带进第 3 步 |
| Human-in-the-Loop | 审批超时（`--timeout-approver`） | fail-safe 默认拒绝，不调用支付网关 |

## 目录结构

```
mm-agent/
├── mm_agent/
│   ├── llm.py            # LLM 抽象：ScriptedLLM（离线假模型）+ OpenAICompatLLM
│   ├── protocol.py       # Thought/Action/Observation 文本协议解析、JSON/代码块提取
│   ├── tools.py          # 工具注册、JSON Schema 参数校验、结构化错误
│   ├── trace.py          # 执行轨迹：终端呈现 + 事件统计 + JSON 落盘
│   ├── state.py          # JSON 状态存储（原子写），支撑暂停与断点续跑
│   ├── pyrun.py          # 子进程跑候选代码与单测（Reflection 的确定性 critic）
│   ├── cli.py            # list / info / run 三个子命令
│   └── patterns/
│       ├── __init__.py   # 七种模式的登记表与统一结果类型
│       ├── tool_use/     # 模式 1：Tool Use
│       │   ├── __init__.py   # 包入口（重导出 run 与公共名）
│       │   ├── __main__.py   # python -m mm_agent.patterns.tool_use
│       │   └── agent.py      # 模式实现
│       ├── react/  reflection/  planning/  multi_agent/  sequential/
│       │   └── （同上：__init__.py + __main__.py + agent.py）
│       └── human_in_the_loop/    # 模式 7：一个模式一个目录，内部按职责再分文件
│           ├── __init__.py       # 包入口（重导出 run 与审批/业务域类型）
│           ├── __main__.py       # python -m mm_agent.patterns.human_in_the_loop
│           ├── agent.py          # 流程编排：阶段推进、暂停、续跑、审计留痕
│           ├── approval.py       # 闸门与审批通道（终端/自动/超时）
│           └── refund.py         # 退款业务域：订单/客户/支付 mock 与政策判定
├── tests/                # 43 个单测
├── PATTERNS.md           # 每个模式的核心循环、看点、代码位置、实测输出
└── SOURCES.md            # 来源、出处映射、假设与未验证项
```

每个模式目录都遵循同一约定：`agent.py` 放模式实现，`__init__.py` 重导出公共名（`run` 等），
`__main__.py` 提供 `python -m mm_agent.patterns.<模式>` 单跑入口。因此新增一个模式只需加一个目录，
并在 [patterns/__init__.py](mm_agent/patterns/__init__.py) 的登记表里补一行。

## 内核为什么这么小

七种模式的差异在**控制流**，不在基础设施。所以内核只做四件共用的事：

1. `llm.py`：把「模型调用」抽象成 `LLM.chat`。假模型是规则匹配的确定性实现，只用于让循环
   在离线可跑、可复现；它不代表真实模型的语义能力。
2. `tools.py`：工具是唯一的外部副作用入口。`ToolRegistry.call` **不抛异常**，参数不合法或执行
   失败都返回可读错误，模型才能据此自我纠正（工具静默失败会被模型当成「没有数据」而编造）。
3. `trace.py` + `state.py`：前者让每一步可观测，后者让流程可暂停、可续跑。
4. `protocol.py`：文本协议解析。真实系统可用原生 function calling，这里用文本是为了把每一步
   原样打印出来。

## 换成真实模型

```bash
export MM_AGENT_API_KEY=sk-...
export MM_AGENT_MODEL=gpt-4o-mini          # 可选
export MM_AGENT_BASE_URL=https://api.openai.com/v1   # 可选，兼容自建网关
python3 -m mm_agent run react --llm openai
```

各模式对模型输出的契约要求写在提示词里：ReAct/Tool Use 用 `Thought/Action/Action Input/
Final Answer`；Planning 与 Sequential 要求输出 JSON；Reflection 要求输出 `python` 代码块；
Multi-Agent 的编排者要求输出分工 JSON。真实模型下这些解析失败会走与假模型相同的错误路径
（`ProtocolError` → 纠正提示 → 重试或终止）。

**未验证**：本仓库未在真实模型上跑过（环境无 API Key），真实模型的输出稳定性属于未验证项，
见 [SOURCES.md](SOURCES.md#假设与未验证项)。

## 验证证据

| 命令 | 实测结果（2026-11-22） |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | `Ran 43 tests` → `OK` |
| `python3 -m mm_agent run all` | 7/7 成功；各模式步数/模型调用：tool-use 4/4、react 5/5、reflection 2/2、planning 6/3、multi-agent 2/8、sequential 5/3、human-in-the-loop 8/1 |
| `python3 -m mm_agent run sequential --bad-input` | 在第 2 步「生成提纲」中断，`failed_step=生成提纲` |
| `python3 -m mm_agent run human-in-the-loop` → `--resume --reject` → `--resume --approve` | 第一次暂停零副作用；拒绝后不退款；被拒绝的申请续跑也不会重新尝试 |

## 已知限制

1. 假模型是**规则匹配**，只演示循环、护栏与失败路径，不能用来评估提示词质量。
2. 真实模型路径未验证（无 API Key）。
3. 工具都是内存 mock：没有并发、重试、限流、幂等键，也没有真实的外部系统。
4. Human-in-the-Loop 的「审批超时」用 `TimeoutApprover` 模拟，没有真实计时器；审批通道只在
   终端与自动审批之间切换，没有接工单/IM。
5. 与 Anthropic 分类对比后可看到覆盖缺口：`routing`（分类路由）与 `parallelization`
   （分片/投票）不在主流「七种模式」清单里，本仓库因此未实现。
6. 模式组合只演示了「多智能体内部复用 ReAct 循环」，其余组合（如 ReAct + Reflection +
   Planning）没有逐一实现。
