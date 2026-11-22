"""命令行入口。

- ``mm-agent list``：列出七个模式
- ``mm-agent info <模式>``：查看某个模式的适用场景与坑
- ``mm-agent run <模式|all>``：运行示例（默认用离线假模型，``--llm openai`` 切真实模型）
- ``python -m mm_agent.patterns.<模块>``：等价于单跑该模式

约定：退出码 0 表示成功，1 表示失败或需要人工介入（如 Human-in-the-Loop 暂停）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .patterns import PATTERNS, PatternInfo, PatternResult, get_pattern, run_pattern
from .trace import Trace


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", default=None, help="覆盖该模式的默认任务描述")
    parser.add_argument(
        "--llm",
        default="scripted",
        choices=("scripted", "openai"),
        help="scripted=离线确定性假模型（默认），openai=OpenAI 兼容真实模型",
    )
    parser.add_argument("--quiet", action="store_true", help="不打印执行轨迹")
    parser.add_argument("--save-trace", metavar="PATH", default=None, help="把执行轨迹写成 JSON 文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印结果")


def _add_pattern_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-steps", type=int, default=None, help="tool-use / react：最大步数")
    parser.add_argument("--max-iterations", type=int, default=None, help="reflection：最大反思轮数")
    parser.add_argument("--keep-workdir", action="store_true", help="reflection：保留单测临时目录便于排查")
    parser.add_argument("--max-rounds", type=int, default=None, help="planning：最大（重）规划轮数")
    parser.add_argument("--max-write-rounds", type=int, default=None, help="multi-agent：最大重写轮数")
    parser.add_argument("--bad-input", action="store_true", help="sequential：注入坏提纲，演示契约中断")
    parser.add_argument("--state", metavar="PATH", default=None, help="planning / human-in-the-loop：状态文件路径")
    parser.add_argument("--resume", action="store_true", help="planning / human-in-the-loop：从状态文件续跑")
    parser.add_argument("--approve", action="store_true", help="human-in-the-loop：自动批准所有闸门")
    parser.add_argument("--reject", action="store_true", help="human-in-the-loop：自动拒绝所有闸门")
    parser.add_argument("--interactive", action="store_true", help="human-in-the-loop：在终端交互式审批")
    parser.add_argument("--timeout-approver", action="store_true", help="human-in-the-loop：模拟审批无人响应")


def _options(args: argparse.Namespace) -> dict[str, Any]:
    """把命令行参数翻译成模式模块的 ``**options``；未显式给出的参数不传。"""

    options: dict[str, Any] = {}
    if getattr(args, "max_steps", None) is not None:
        options["max_steps"] = args.max_steps
    if getattr(args, "max_iterations", None) is not None:
        options["max_iterations"] = args.max_iterations
    if getattr(args, "keep_workdir", False):
        options["keep_workdir"] = True
    if getattr(args, "max_rounds", None) is not None:
        options["max_rounds"] = args.max_rounds
    if getattr(args, "max_write_rounds", None) is not None:
        options["max_write_rounds"] = args.max_write_rounds
    if getattr(args, "bad_input", False):
        options["broken_outline"] = True
    if getattr(args, "state", None):
        options["state_path"] = args.state
    if getattr(args, "resume", False):
        options["resume"] = True
    if getattr(args, "interactive", False):
        options["interactive"] = True

    if getattr(args, "approve", False) or getattr(args, "reject", False) or getattr(args, "timeout_approver", False):
        from .patterns.human_in_the_loop import AutoApprover, TimeoutApprover

        if getattr(args, "approve", False):
            options["approver"] = AutoApprover(approved=True, by="cli-approver", reason="命令行 --approve")
        elif getattr(args, "reject", False):
            options["approver"] = AutoApprover(approved=False, by="cli-approver", reason="命令行 --reject")
        else:
            options["approver"] = TimeoutApprover()
    return options


def _print_result(result: PatternResult, trace: Trace, info: PatternInfo, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "pattern": result.pattern,
            "task": result.task,
            "ok": result.ok,
            "steps": result.steps,
            "llm_calls": result.llm_calls,
            "notes": result.notes,
            "output": result.output,
            "extra": result.extra,
            "trace": {"events": len(trace.events), "counts": dict(sorted(trace.counts.items()))},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    print(f"\n{result.summary_line()}")
    if result.notes:
        for note in result.notes:
            print(f"  - {note}")
    if result.output:
        print("\n--- 产出 ---")
        print(result.output if len(result.output) < 2000 else result.output[:2000] + "\n…（已截断）")
    print(f"\n轨迹统计：{trace.summary()}")
    print(f"提示：{info.zh_name} 的适用场景 —— {info.when_to_use}")


def main_for(key: str, argv: Sequence[str] | None = None) -> int:
    """单模式入口；各模式模块的 ``__main__`` 都走这里。"""

    info = get_pattern(key)
    parser = argparse.ArgumentParser(
        prog=f"mm-agent run {info.key}",
        description=f"{info.zh_name}（{info.name}）：{info.summary}",
    )
    _add_common_flags(parser)
    _add_pattern_flags(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    trace = Trace(f"{info.zh_name}（{info.name}）", stream=None if args.quiet else sys.stdout)
    result = run_pattern(info.key, task=args.task, llm_kind=args.llm, trace=trace, **_options(args))
    if args.save_trace:
        path = trace.dump(args.save_trace)
        print(f"轨迹已写入 {path}")
    _print_result(result, trace, info, as_json=args.json)
    return 0 if result.ok else 1


def _run_all(args: argparse.Namespace) -> int:
    from .patterns.human_in_the_loop import AutoApprover

    print("运行全部七个模式（假模型、安静模式）；仅 human-in-the-loop 使用自动审批者，单跑该模式才会真的停顿等人工。\n")
    failures = 0
    trace_dir = ""
    if args.save_trace:
        trace_dir = args.save_trace.rstrip("/")
    for info in PATTERNS:
        trace = Trace(stream=None)
        options = _options(args)
        if info.key == "human-in-the-loop":
            options["approver"] = AutoApprover(approved=True, by="run-all-demo", reason="批量演示自动审批")
        result = run_pattern(info.key, task=None, llm_kind=args.llm, trace=trace, **options)
        failures += 0 if result.ok else 1
        print(f"{'✔' if result.ok else '✘'} {info.key:<20} {result.summary_line()}")
        for note in result.notes:
            print(f"    - {note}")
        if trace_dir:
            print(f"    轨迹写入 {trace.dump(f'{trace_dir}/{info.key}-trace.json')}")
    print(f"\n合计：{len(PATTERNS) - failures}/{len(PATTERNS)} 成功")
    return 0 if failures == 0 else 1


def _run_one(args: argparse.Namespace) -> int:
    if args.pattern == "all":
        return _run_all(args)
    return main_for(args.pattern, _rebuild_argv(args))


def _rebuild_argv(args: argparse.Namespace) -> list[str]:
    """把已解析的命名空间还原成单模式入口的参数列表，避免两套参数定义。"""

    argv: list[str] = []
    if args.task:
        argv += ["--task", args.task]
    argv += ["--llm", args.llm]
    if args.quiet:
        argv.append("--quiet")
    if args.save_trace:
        argv += ["--save-trace", args.save_trace]
    if args.json:
        argv.append("--json")
    for flag, value in (
        ("--max-steps", args.max_steps),
        ("--max-iterations", args.max_iterations),
        ("--max-rounds", args.max_rounds),
        ("--max-write-rounds", args.max_write_rounds),
        ("--state", args.state),
    ):
        if value is not None:
            argv += [flag, str(value)]
    for flag in ("keep_workdir", "bad_input", "resume", "approve", "reject", "interactive", "timeout_approver"):
        if getattr(args, flag, False):
            argv.append("--" + flag.replace("_", "-"))
    return argv


def _cmd_list() -> int:
    print("Agent 七种设计模式（mm-agent）\n")
    for info in PATTERNS:
        print(f"{info.key:<20} {info.zh_name:<12} {info.name}")
        print(f"{'':<20} {info.summary}")
        print(f"{'':<20} 生产可用度：{info.readiness}")
    print("\n用 mm-agent info <模式> 查看适用场景与坑；用 mm-agent run <模式> 运行示例。")
    return 0


def _cmd_info(pattern: str) -> int:
    info = get_pattern(pattern)
    print(f"{info.zh_name}（{info.name}）")
    print(f"  模块：mm_agent/patterns/{info.module}.py")
    print(f"  一句话：{info.summary}")
    print(f"  默认任务：{info.default_task}")
    print(f"  什么时候用：{info.when_to_use}")
    print(f"  主要坑：{info.pitfall}")
    print(f"  生产可用度：{info.readiness}")
    print(f"  运行：mm-agent run {info.key}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mm-agent", description="Agent 七种设计模式的离线可运行示例集")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出七个模式")

    info_parser = sub.add_parser("info", help="查看某个模式的说明")
    info_parser.add_argument("pattern")

    run_parser = sub.add_parser("run", help="运行示例（模式名或 all）")
    run_parser.add_argument("pattern", nargs="?", default="all", help=f"模式名之一：{', '.join(i.key for i in PATTERNS)}，或 all")
    _add_common_flags(run_parser)
    _add_pattern_flags(run_parser)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "list":
        return _cmd_list()
    if args.command == "info":
        return _cmd_info(args.pattern)
    if args.command == "run":
        return _run_one(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
