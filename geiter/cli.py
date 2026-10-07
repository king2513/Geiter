from __future__ import annotations

import argparse
import json
from typing import Any

from .core import GeiterStore
from .gateway import serve
from .providers import load_provider, resume_provider, run_provider


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="geiter", description="Agent-native GEO self-iteration runtime")
    root.add_argument("--root", default=".", help="Workspace root")
    root.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    commands = root.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("init", "Initialize a Geiter workspace"),
        ("status", "Show workspace status"),
        ("inspect", "Record a workspace observation"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    prompt = commands.add_parser("prompt", help="Manage retrieval prompts")
    prompt.add_argument("action", choices=("add", "list"))
    prompt.add_argument("text", nargs="?")
    prompt.add_argument("--intent")
    prompt.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    observe = commands.add_parser("observe", help="Record a provider observation")
    observe.add_argument("prompt_id")
    observe.add_argument("--provider", required=True)
    observe.add_argument("--answer", required=True)
    observe.add_argument("--citation", action="append", default=[])
    observe.add_argument("--target")
    observe.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    run = commands.add_parser("run", help="Run a provider across all prompts")
    run.add_argument("--provider", required=True)
    run.add_argument("--fixture", help="Path to JSONL provider fixture")
    run.add_argument("--target")
    run.add_argument("--max-attempts", type=int, default=1)
    run.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    resume = commands.add_parser("resume", help="Resume a provider run from its durable ledger")
    resume.add_argument("run_id")
    resume.add_argument("--provider", required=True)
    resume.add_argument("--fixture", help="Path to JSONL provider fixture")
    resume.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    analyze = commands.add_parser("analyze", help="Analyze retrieval observations")
    analyze.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    health = commands.add_parser("health", help="Assess observation and provider quality")
    health.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    matrix = commands.add_parser("matrix", help="Analyze coverage by provider and prompt intent")
    matrix.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    baseline = commands.add_parser("baseline", help="Manage analysis baselines")
    baseline.add_argument("action", choices=("save", "compare"))
    baseline.add_argument("--label")
    baseline.add_argument("--id")
    baseline.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    experiment = commands.add_parser("experiment", help="Propose or approve policy-gated experiments")
    experiment.add_argument("action", choices=("propose", "approve", "result"))
    experiment.add_argument("--id")
    experiment.add_argument("--hypothesis")
    experiment.add_argument("--change")
    experiment.add_argument("--risk", default="low")
    experiment.add_argument("--outcome", choices=("supported", "rejected", "inconclusive"))
    experiment.add_argument("--evidence")
    experiment.add_argument("--baseline-id")
    experiment.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("doctor", help="Validate workspace consistency").add_argument(
        "--json", action="store_true", help=argparse.SUPPRESS
    )
    commands.add_parser("report", help="Emit a complete GEO report").add_argument(
        "--json", action="store_true", help=argparse.SUPPRESS
    )
    gateway = commands.add_parser("gateway", help="Serve the stdio agent gateway")
    gateway.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    connect = commands.add_parser("connect", help="Print agent client connection configuration")
    connect.add_argument("--format", choices=("generic", "claude", "cursor", "vscode"), default="generic")
    connect.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    iterate = commands.add_parser("iterate", help="Run one self-iteration cycle")
    iterate.add_argument("--hypothesis")
    iterate.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    goal = commands.add_parser("goal", help="Manage goals")
    goal.add_argument("action", choices=("add", "list"))
    goal.add_argument("text", nargs="?")
    goal.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    memory = commands.add_parser("memory", help="Manage durable memory")
    memory.add_argument("action", choices=("add", "list"))
    memory.add_argument("kind", nargs="?")
    memory.add_argument("text", nargs="?")
    memory.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    event = commands.add_parser("event", help="Inspect the event log")
    event.add_argument("action", choices=("list",))
    event.add_argument("--limit", type=int, default=20)
    event.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return root


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else item}")
    elif isinstance(value, list):
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(value)


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    store = GeiterStore(args.root)
    as_json = args.json or getattr(args, "json", False)

    if args.command == "init":
        result = store.init()
    elif args.command == "status":
        state = store.read()
        result = {
            "schema": state["schema"],
            "identity": state["identity"],
            "updated_at": state["updated_at"],
            "counts": {key: len(state.get(key, [])) for key in (
                "goals", "memories", "prompts", "observations", "hypotheses",
            "actions", "measurements", "learnings", "iterations", "baselines", "experiments",
                "runs",
            )},
        }
    elif args.command == "inspect":
        result = store.inspect()
    elif args.command == "prompt":
        if args.action == "add":
            if not args.text:
                raise SystemExit("prompt add requires text")
            result = store.add_prompt(args.text, args.intent)
        else:
            result = store.list_records("prompts")
    elif args.command == "observe":
        result = store.record_observation(
            args.prompt_id,
            args.provider,
            args.answer,
            args.citation,
            args.target,
        )
    elif args.command == "run":
        if args.provider == "jsonl" and not args.fixture:
            raise SystemExit("run --provider jsonl requires --fixture")
        provider = load_provider(args.provider, path=args.fixture)
        result = {
            "provider": provider.name,
            **run_provider(store, store.prompts(), provider, args.target, args.max_attempts),
        }
    elif args.command == "resume":
        if args.provider == "jsonl" and not args.fixture:
            raise SystemExit("resume --provider jsonl requires --fixture")
        provider = load_provider(args.provider, path=args.fixture)
        result = {
            "provider": provider.name,
            **resume_provider(store, args.run_id, provider),
        }
    elif args.command == "analyze":
        result = store.analyze()
    elif args.command == "health":
        result = store.health()
    elif args.command == "matrix":
        result = store.analyze_matrix()
    elif args.command == "baseline":
        result = store.save_baseline(args.label) if args.action == "save" else store.compare(args.id)
    elif args.command == "experiment":
        if args.action == "propose":
            if not args.hypothesis or not args.change:
                raise SystemExit("experiment propose requires --hypothesis and --change")
            result = store.propose_experiment(args.hypothesis, args.change, args.risk)
        else:
            if not args.id:
                raise SystemExit(f"experiment {args.action} requires --id")
            if args.action == "approve":
                result = store.approve_experiment(args.id)
            else:
                if not args.outcome or not args.evidence:
                    raise SystemExit("experiment result requires --outcome and --evidence")
                result = store.record_experiment_result(args.id, args.outcome, args.evidence, args.baseline_id)
    elif args.command == "doctor":
        result = store.doctor()
    elif args.command == "report":
        result = store.report()
    elif args.command == "gateway":
        serve(args.root)
        return
    elif args.command == "connect":
        base = store.connection_config()
        if args.format == "generic":
            result = base
        elif args.format == "claude":
            result = {"mcpServers": {"geiter": {"command": base["command"], "args": base["args"]}}}
        elif args.format == "cursor":
            result = {"mcpServers": {"geiter": {"command": base["command"], "args": base["args"]}}}
        else:
            result = {"servers": {"geiter": {"type": "stdio", "command": base["command"], "args": base["args"]}}}
    elif args.command == "iterate":
        result = store.iterate(args.hypothesis)
    elif args.command == "goal":
        if args.action == "add":
            if not args.text:
                raise SystemExit("goal add requires text")
            result = store.add("goals", "goal", {"text": args.text, "status": "active"})
        else:
            result = store.list_records("goals")
    elif args.command == "memory":
        if args.action == "add":
            if not args.kind or not args.text:
                raise SystemExit("memory add requires kind and text")
            result = store.add("memories", args.kind, {"text": args.text})
        else:
            result = store.list_records("memories")
    elif args.command == "event":
        result = store.events(args.limit)
    else:
        raise SystemExit(f"unknown command: {args.command}")

    emit(result, as_json)


if __name__ == "__main__":
    main()
