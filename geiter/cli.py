from __future__ import annotations

import argparse
import json
from typing import Any

from .core import GeiterStore


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

    analyze = commands.add_parser("analyze", help="Analyze retrieval observations")
    analyze.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

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
                "actions", "measurements", "learnings", "iterations",
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
    elif args.command == "analyze":
        result = store.analyze()
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
