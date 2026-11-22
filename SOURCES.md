# 来源、出处与未验证项

抓取时间：2026-11-22。所有外部内容按「数据」使用，只取用与实际抓取到的正文一致的部分。

## 来源清单

| 编号 | 来源 | 抓取状态 | 本仓库取用了什么 | 未取用什么 |
| --- | --- | --- | --- | --- |
| S1 | [The 7 Agentic AI Design Patterns Every Developer Should Know — dev.to / Rishabh Sethia](https://dev.to/emperorakashi20/the-7-agentic-ai-design-patterns-every-developer-should-know-react-reflection-tool-use-and-more-3bba) | 正文已抓取 | 七种模式清单；每个模式的 gotcha；生产可用度评分表；「多数任务用单个 ReAct + 好工具即可」的判断 | 文中提到的客户案例细节、n8n/LangGraph/CrewAI 的具体实现 |
| S2 | [AI Agent 七大设计模式 — DarryPy](https://darrypy.github.io/posts/2026-05-21-ai-agent-design-patterns.html) | 正文已抓取 | 七种模式的中文口径；ReAct 与 Plan-and-Execute 的区别；Multi-Agent 的四种子形态（Sequential / Parallel / Hierarchical / Debate）；「先用最简模式」的选型判断 | 文中引用的「Plan-and-Execute 完成率 92%、快 3.6 倍」等实测数字（未复核原始研究，本仓库未采用） |
| S3 | [7 Must-Know Agentic AI Design Patterns — MachineLearningMastery](https://machinelearningmastery.com/7-must-know-agentic-ai-design-patterns/) | **正文抓取时被截断**（仅拿到标题与站点框架） | 仅取用搜索结果摘要中给出的模式清单（ReAct、Reflection、Planning、Tool Use、Multi-Agent Collaboration、Sequential Workflows、Human-in-the-Loop），作为 S1 的**独立第二来源** | 其余内容一律未采用 |
| S4 | [Building effective agents — Anthropic](https://www.anthropic.com/research/building-effective-agents) | 正文已抓取 | workflows 与 agents 的区分；六种形态名称与适用场景（prompt chaining / routing / parallelization / orchestrator-workers / evaluator-optimizer / autonomous agent）；「Agents are typically just LLMs using tools based on environmental feedback in a loop」「stopping conditions (such as a maximum number of iterations)」「pause for human feedback at checkpoints」；工具提示词工程建议（Appendix 2） | 具体框架与 SDK 推荐、SWE-bench 案例细节 |
| — | [Common workflow patterns for AI agents — Claude/Anthropic](https://claude.com/resources/articles/common-workflow-patterns-for-ai-agents-and-when-to-use-them) | **未抓取** | 未采用 | 全部 |

时效性说明：S4 页面顶部声明该文写于 2024-12，工具链已有变化，并指向 Anthropic 的 Managed
Agents 文档。本仓库取用的是其中的**架构分类**，未取用任何具体产品/接口细节。

## 结论 → 出处映射

| 结论 | 出处 |
| --- | --- |
| 「七种模式」这个清单本身（ReAct / Reflection / Planning / Tool Use / Multi-Agent / Sequential Workflows / Human-in-the-Loop） | S1 正文 + S3 搜索摘要（两个独立来源一致） |
| 各模式「生产可用度」分档（如 Multi-Agent「谨慎使用」、Sequential「生产可用」） | S1 的评分表（**该文作者的评估**，非行业标准，本仓库照引并标注归属） |
| 各模式「主要坑」（ReAct 死循环与上下文膨胀、Reflection 标准含糊、Planning 计划错误、Multi-Agent 协调失败、Sequential 不自适应、HITL 审批瓶颈） | S1 正文 + S2 正文 |
| ReAct 循环写法 `Thought → Action → Observation` | S1 + S2 |
| Multi-Agent 的 Sequential / Parallel / Hierarchical / Debate 子形态 | S2 |
| 「先用最简模式，跑不通再升级」的选型顺序 | S2（本仓库在 [PATTERNS.md](PATTERNS.md#选型顺序) 复述） |
| workflows 与 agents 的区分、六种工作流形态的名称与适用场景 | S4 |
| ReAct 需要停止条件（最大迭代次数）这条护栏建议 | S4（`stopping conditions (such as a maximum number of iterations)`） |
| HITL 是「在检查点暂停等人工反馈」 | S4（`pause for human feedback at checkpoints`） |
| 工具需要好的 ACI：示例用法、边界、参数命名清晰、poka-yoke | S4 Appendix 2 |
| 「本仓库模式 ↔ Anthropic 形态」的对应关系 | **本仓库判断**，不是 S4 的结论（[PATTERNS.md](PATTERNS.md#与-anthropic-工作流分类的对应关系) 已标注） |
| 每个示例任务的场景、注入的失败路径、代码结构 | 本仓库自写 |
| 所有步数、模型调用次数、字数、单测数量等数字 | 本仓库实跑所得，命令见下 |

## 本仓库自查数据（可复现）

| 命令 | 结果摘要 |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | `Ran 43 tests` → `OK` |
| `python3 -m mm_agent run all` | 7/7 成功；tool-use 4 步/4 调用、react 5/5、reflection 2/2、planning 6/3、multi-agent 2/8、sequential 5/3、human-in-the-loop 8/1 |
| `python3 -m mm_agent run sequential --bad-input` | `failed_step=生成提纲`、`steps_done=1` |
| `python3 -m mm_agent run human-in-the-loop` | 暂停、零副作用、`status=waiting_approval` |

自查数据对应的关键代码位置：工具校验 [tools.py:136](mm_agent/tools.py#L136)、
文本协议 [protocol.py:51](mm_agent/protocol.py#L51)、轨迹 [trace.py](mm_agent/trace.py)、
状态存储 [state.py:20](mm_agent/state.py#L20)、子进程跑单测 [pyrun.py:47](mm_agent/pyrun.py#L47)、
ReAct 护栏 [react/agent.py:250](mm_agent/patterns/react/agent.py#L250)、
计划校验 [planning/agent.py:82](mm_agent/patterns/planning/agent.py#L82)、
多智能体机械校验 [multi_agent/agent.py:130](mm_agent/patterns/multi_agent/agent.py#L130)、
顺序管道契约 [sequential/agent.py:149](mm_agent/patterns/sequential/agent.py#L149)、
审批闸门默认拒绝 [human_in_the_loop/approval.py:22](mm_agent/patterns/human_in_the_loop/approval.py#L22)、
退款政策判定 [human_in_the_loop/refund.py:129](mm_agent/patterns/human_in_the_loop/refund.py#L129)。

## 假设与未验证项

1. **真实模型未验证**：本仓库未在真实模型上运行过（环境无 API Key）。提示词中写明了输出契约，
   但真实模型是否稳定遵循、各模式的失败率如何，均未验证。
2. **假设：假模型足以演示模式行为**。规则匹配的假模型能稳定复现循环、护栏与失败路径，
   但不能替代对提示词质量与模型能力的评估。
3. **示例数据均为虚构**：订单 O-7788、客户 C-1024、金额 8600 元、`api-gateway` 日志、
   销售数据 128/260 行等，都是为了让失败路径可复现而构造的假数据，不对应任何真实系统。
4. **政策口径为示例值**：退款高风险阈值 5000 元、无理由窗口 30 天来自示例设定，不是任何
   真实业务规则（[human_in_the_loop/refund.py](mm_agent/patterns/human_in_the_loop/refund.py#L15-L17) 已标注）。
5. **S3 只用到模式清单**：该页面正文抓取被截断，除搜索摘要里的清单外未采用其它内容。
6. **未实现的模式**：S4 中的 routing 与 parallelization 不在主流「七种模式」清单内，本仓库
   未实现；如需覆盖，可作为第 8、9 个示例补充。
7. **S1 的量化说法未复核**：文中「Reflection 让代码生成正确率提升 15~25%」「Plan-and-Execute
   完成率 92%、快 3.6 倍」等数字未追溯到原始研究，本仓库未采用，也不作为结论。
