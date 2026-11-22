[2026-11-22 18:20] - 修改

# mm-agent：Agent 七种设计模式示例集（新建项目）

## 目标与范围

- 目标：在 `/Users/wch/opensource/mm-agent` 新建一个可离线运行的 Agent 七种设计模式示例集，
  每个模式一个可独立运行的实例（用户要求「抓取或自写均可，默认七种」）。
- 非目标：不接入真实业务系统、不做生产级并发/限流/幂等，不提交 Git，不修改该目录之外的任何文件。
- 模式口径：以网上主流的「七种模式」清单为准（Tool Use / ReAct / Reflection / Planning /
  Multi-Agent / Sequential Workflows / Human-in-the-Loop），来源见 `SOURCES.md`（S1~S4）。

## 关键决策

- 语言与依赖：Python ≥ 3.10，纯标准库，零第三方依赖，便于直接运行。
- 离线可跑：默认 `ScriptedLLM`（规则匹配的确定性假模型），保证示例、断言与输出可复现；
  真实模型通过 `--llm openai`（OpenAI 兼容接口）切换，属未验证项。
- 每模式注入一条真实失败路径（而非只演示顺利路径）：参数校验自纠、重复动作护栏、单测驱动的反思、
  计划校验 + 重规划、审稿打回重写、管道契约中断、审批 fail-safe 拒绝。
- 假模型未命中规则时返回兜底文本，不猜测模型行为；提示词模板保留规则依赖的标记。

## 修改文件与影响

新建 20 个文件，均位于 `/Users/wch/opensource/mm-agent`：

- 内核：`mm_agent/llm.py`、`protocol.py`、`tools.py`、`trace.py`、`state.py`、`pyrun.py`、`cli.py`、
  `__init__.py`、`__main__.py`
- 模式：`mm_agent/patterns/__init__.py`、`tool_use.py`、`react.py`、`reflection.py`、`planning.py`、
  `multi_agent.py`、`sequential.py`、`human_in_the_loop.py`、`hitl_approval.py`、`hitl_refund.py`
- 测试与工程：`tests/__init__.py`、`tests/test_kernel.py`、`tests/test_patterns.py`、`pyproject.toml`、`.gitignore`
- 文档：`README.md`、`PATTERNS.md`、`SOURCES.md`

影响面：新目录，无既有代码/数据/接口受影响；无存量数据迁移问题。

## 实施中发现并修复的问题

1. `planning` 重规划时校验把「已完成步骤」判为不存在依赖：`validate_plan` 增加 `completed` 入参，
   断点续跑/重规划可依赖已完成步骤；规则顺序调整为先匹配重规划，避免重规划被「修正计划」规则抢占。
2. `human_in_the_loop.py` 单文件 549 行，超过 500 行约束：按职责拆为 `hitl_approval.py`（闸门与审批通道）、
   `hitl_refund.py`（业务域与政策判定）、`human_in_the_loop.py`（流程编排），并在编排文件重导出类型供 CLI/测试使用。
   拆分后遗漏政策常量，已补回 `hitl_refund.py`。
3. `human-in-the-loop` 的 `llm_calls` 恒为 0（实际对外文案由模型起草）：改为透传真实调用次数；
   `steps` 由「审批次数」改为「已完成阶段数」。
4. 工具参数校验错误信息重复拼接签名：收敛为一条。

## 验证（实测）

- `python3 -m unittest discover -s tests -t .` → `Ran 43 tests`，`OK`。
- `python3 -m mm_agent run all` → 7/7 成功；步数/模型调用：tool-use 4/4、react 5/5、reflection 2/2、
  planning 6/3、multi-agent 2/8、sequential 5/3、human-in-the-loop 8/1。
- `python3 -m mm_agent run sequential --bad-input` → 第 2 步「生成提纲」中断，`steps_done=1`。
- `python3 -m mm_agent run human-in-the-loop` → 暂停、零副作用；`--resume --reject` 不退款；
  被拒绝后 `--resume --approve` 仍不重试；`--timeout-approver` 由 system 按 fail-safe 拒绝。
- `python3 -m mm_agent list` / `info planning` / `python3 -m mm_agent.patterns.react` / `--json` 均正常。

## 未完成项与风险

- 真实模型路径未验证（无 API Key）：提示词中的输出契约（ReAct 文本协议、JSON 计划、代码块）在真实模型下的
  稳定性未验证，见 `SOURCES.md` 假设与未验证项 1、2。
- 未实现 Anthropic 分类中的 routing 与 parallelization（不在主流七种清单内）。
- 假模型是规则匹配，只演示循环与护栏，不能用于评估提示词质量。
- 未做 Git 初始化/提交（用户未授权）。

[2026-11-22 18:28] - 修改

## mm-agent 复查阶段（代码审计与清理）

5. `react.py` 遗留未使用导入 `AgentStep`：删除（临时 AST 检查发现）。
6. `sequential.py` 的 `ContractError` 定义后未被使用（死代码）：删除；同时补上步骤内的
   `ProtocolError` 处理——模型输出不含合法 JSON 时按「该步输出无法解析」精确定位中断，
   避免异常直接冒到 CLI。
7. `patterns/__init__.py` 中 reflection 的 `default_task` 与模块 `DEFAULT_TASK` 文案不一致：对齐。

### 复查验证

- 临时静态检查（AST 脚本写在会话临时目录，未写入仓库）：未使用导入 / 顶层重复定义 / TODO 标记
  → 修复后 0 条；该检查为启发式，不替代完整 linter（环境无 ruff / pyflakes）。
- `python3 -m compileall -q mm_agent tests` → 通过。
- 复查后重跑：43 个单测 `OK`；`run all` 7/7 成功；`sequential --bad-input` 仍在第 2 步中断。
- 清理验证产生的 `__pycache__`；保留 `runs/` 下 planning 与 hitl 的状态文件（已被 `.gitignore` 覆盖）。

[2026-11-22 18:32] - 修改

## mm-agent 结构调整：每个模式一个目录

依据用户要求「每一种一个目录」：

1. 每个模式一个包目录 `mm_agent/patterns/<模式>/`，统一含 `agent.py`（模式实现）、`__init__.py`
   （包入口，重导出 `run` 与公共名）、`__main__.py`（`python -m mm_agent.patterns.<模式>` 单跑入口）。
2. HITL 原有三个文件随之归入自身目录：`human_in_the_loop/agent.py`（流程编排）、`approval.py`
   （闸门与审批通道）、`refund.py`（业务域与政策判定），文件名去掉 `hitl_` 前缀。
3. 模块内相对导入层级 +1（`from ..llm` → `from ...llm`；`from . import PatternResult` →
   `from .. import PatternResult`）；`agent.py` 尾部的 `if __name__ == "__main__"` 块移除，改由各目录
   `__main__.py` 承担。
4. 登记表 `patterns/__init__.py` 的模块名与 `load_module` / `run_pattern` 契约不变（仍按
   `mm_agent.patterns.<模式>` 导入包）。
5. 测试：`human_in_the_loop.JsonStateStore` 改为从 `mm_agent.state` 导入（不再经由模式包转出）。
6. 文档：README 目录结构改为目录树；PATTERNS.md / SOURCES.md 的代码位置链接与标签同步为新路径；
   sequential 因新增 `ProtocolError` 处理导致行号位移，同步修正（`run_pipeline` 153→149、
   `build_pipeline` 226→227）。

### 验证

- 43 个单测 `OK`；`python3 -m compileall -q mm_agent tests` 通过；临时 AST 检查（未使用导入 / 重复定义）0 条。
- `python3 -m mm_agent run all` → 7/7 成功。
- 七个目录入口 `python3 -m mm_agent.patterns.<模式>` 全部 `exit=0`。
- 文档代码链接校验：34 条链接目标存在、行号有效、抽查内容与预期一致。
- 清理验证产生的 `__pycache__`。

[2026-11-22 18:43] - 修改

## mm-agent 补充：ASCII 架构图

依据用户要求「用 ASCII 画对应的 Agent 架构图」：

1. 在 `PATTERNS.md` 末尾新增「## 架构图（ASCII）」一节，共 10 张图：全景（一个内核 + 七个
   模式目录）、与 Agent 架构图分类的对应关系、七个模式各一张、选型阶梯。每张图注明依据的
   代码文件；外部分类口径标注 [S4]，选型判断标注 [S2]。
2. 图由会话内临时脚本生成（盒子宽度按显示宽度计算，CJK 按 2 列），生成时自检「盒线宽度
   一致、无超宽行、无行尾空白」；脚本只写入临时目录，未进入仓库。
3. `README.md` 增加指向该节的说明。
4. 文档自检：34 条代码链接有效、代码块围栏成对、10 张架构图盒线宽度一致。

### 验证

- 43 个单测 `OK`；`python3 -m mm_agent run all` → 7/7 成功（纯文档改动，跑一遍确认无意外）。
- 目录结构、代码、状态文件格式均未变化。
- 补充：图写入后又做了一次折行微调（Tool Use 首行、Sequential 步骤行），复查后 10 张图仍为
  「盒线对齐、无超宽行、无行尾空白」；单测重跑 `OK`。
