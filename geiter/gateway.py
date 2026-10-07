"""A tiny stdio JSON-RPC gateway for agent and MCP-style callers.

This is intentionally transport-only. The domain contract stays in GeiterStore,
so an MCP server or HTTP adapter can be added later without duplicating logic.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .core import GeiterStore
from .providers import load_provider, regression_run, resume_provider


TOOLS = [
    {
        "name": "geiter_status",
        "description": "Read the current Geiter workspace summary.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_add_prompt",
        "description": "Add or reuse a deterministic retrieval prompt.",
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}, "intent": {"type": "string"}},
        },
    },
    {
        "name": "geiter_record_observation",
        "description": "Record a provider answer and citations for a prompt.",
        "inputSchema": {
            "type": "object",
            "required": ["prompt_id", "provider", "answer"],
            "properties": {
                "prompt_id": {"type": "string"},
                "provider": {"type": "string"},
                "answer": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
                "target": {"type": "string"},
            },
        },
    },
    {
        "name": "geiter_analyze",
        "description": "Compute GEO retrieval signals and recommend the next action.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_health",
        "description": "Assess observation quality and provider health before trusting metrics.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_runs",
        "description": "Read recent provider run ledgers and their success/failure summaries.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_connect",
        "description": "Return a portable stdio configuration for an agent client.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["generic", "claude", "cursor", "vscode"]},
            },
        },
    },
    {
        "name": "geiter_resume",
        "description": "Resume a provider run using only prompts still eligible for retry.",
        "inputSchema": {
            "type": "object",
            "required": ["run_id", "provider"],
            "properties": {
                "run_id": {"type": "string"},
                "provider": {"type": "string"},
                "fixture": {"type": "string"},
            },
        },
    },
    {
        "name": "geiter_regression",
        "description": "Run a deterministic provider batch and return its quality gate.",
        "inputSchema": {
            "type": "object",
            "required": ["provider"],
            "properties": {
                "provider": {"type": "string"},
                "fixture": {"type": "string"},
                "target": {"type": "string"},
            },
        },
    },
    {
        "name": "geiter_matrix",
        "description": "Analyze retrieval coverage by provider and prompt intent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_report",
        "description": "Return a complete machine-readable GEO report.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "geiter_save_baseline",
        "description": "Persist the current analysis as a comparison baseline.",
        "inputSchema": {"type": "object", "properties": {"label": {"type": "string"}}},
    },
    {
        "name": "geiter_compare",
        "description": "Compare current retrieval metrics with a saved baseline.",
        "inputSchema": {"type": "object", "properties": {"baseline_id": {"type": "string"}}},
    },
    {
        "name": "geiter_propose_experiment",
        "description": "Propose a policy-gated experiment without external side effects.",
        "inputSchema": {
            "type": "object",
            "required": ["hypothesis", "change"],
            "properties": {
                "hypothesis": {"type": "string"},
                "change": {"type": "string"},
                "risk": {"type": "string"},
            },
        },
    },
    {
        "name": "geiter_record_experiment_result",
        "description": "Record an approved experiment outcome and comparison evidence.",
        "inputSchema": {
            "type": "object",
            "required": ["experiment_id", "outcome", "evidence"],
            "properties": {
                "experiment_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["supported", "rejected", "inconclusive"]},
                "evidence": {"type": "string"},
                "baseline_id": {"type": "string"},
            },
        },
    },
    {
        "name": "geiter_iterate",
        "description": "Run one evidence-aware self-iteration cycle.",
        "inputSchema": {
            "type": "object",
            "properties": {"hypothesis": {"type": "string"}},
        },
    },
]

RESOURCES = [
    {
        "uri": "geiter://status",
        "name": "Geiter status",
        "description": "Current workspace identity and collection counts.",
        "mimeType": "application/json",
    },
    {
        "uri": "geiter://report",
        "name": "Geiter report",
        "description": "Current GEO analysis, health checks, and latest iteration.",
        "mimeType": "application/json",
    },
]


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "json", "json": value}]}}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(store: GeiterStore, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "geiter", "version": "1.8.1"},
            "capabilities": {"tools": {}},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "resources/list":
        return _result(request_id, {"resources": RESOURCES})
    if method == "resources/read":
        uri = params.get("uri")
        if uri == "geiter://status":
            state = store.read()
            value = {
                "schema": state["schema"],
                "identity": state["identity"],
                "updated_at": state["updated_at"],
                "counts": {key: len(state.get(key, [])) for key in (
                    "goals", "memories", "prompts", "observations", "hypotheses",
                "actions", "measurements", "learnings", "iterations",
                "baselines", "experiments",
                )},
            }
        elif uri == "geiter://report":
            value = store.report()
        else:
            return _error(request_id, -32002, f"unknown resource: {uri}")
        return _result(request_id, {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value, ensure_ascii=False)}]})
    if method != "tools/call":
        return _error(request_id, -32601, f"unknown method: {method}")

    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        if name == "geiter_status":
            state = store.read()
            value = {
                "schema": state["schema"],
                "identity": state["identity"],
                "updated_at": state["updated_at"],
                "counts": {key: len(state.get(key, [])) for key in (
                    "goals", "memories", "prompts", "observations", "hypotheses",
                    "actions", "measurements", "learnings", "iterations",
                )},
            }
        elif name == "geiter_add_prompt":
            value = store.add_prompt(args["text"], args.get("intent"))
        elif name == "geiter_record_observation":
            value = store.record_observation(
                args["prompt_id"], args["provider"], args["answer"],
                args.get("citations", []), args.get("target"),
            )
        elif name == "geiter_analyze":
            value = store.analyze()
        elif name == "geiter_health":
            value = store.health()
        elif name == "geiter_runs":
            value = store.list_records("runs")[-20:]
        elif name == "geiter_connect":
            connection = store.connection_config()
            output_format = args.get("format", "generic")
            if output_format == "generic":
                value = connection
            elif output_format in {"claude", "cursor"}:
                value = {"mcpServers": {"geiter": {"command": connection["command"], "args": connection["args"]}}}
            elif output_format == "vscode":
                value = {"servers": {"geiter": {"type": "stdio", "command": connection["command"], "args": connection["args"]}}}
            else:
                raise ValueError(f"unsupported connection format: {output_format}")
        elif name == "geiter_resume":
            provider_name = args["provider"]
            if provider_name == "jsonl" and not args.get("fixture"):
                raise ValueError("geiter_resume with jsonl requires fixture")
            value = {
                "provider": provider_name,
                **resume_provider(
                    store,
                    args["run_id"],
                    load_provider(provider_name, path=args.get("fixture")),
                ),
            }
        elif name == "geiter_regression":
            provider_name = args["provider"]
            if provider_name == "jsonl" and not args.get("fixture"):
                raise ValueError("geiter_regression with jsonl requires fixture")
            value = {
                "provider": provider_name,
                **regression_run(
                    store,
                    store.prompts(),
                    load_provider(provider_name, path=args.get("fixture")),
                    args.get("target"),
                ),
            }
        elif name == "geiter_matrix":
            value = store.analyze_matrix()
        elif name == "geiter_report":
            value = store.report()
        elif name == "geiter_save_baseline":
            value = store.save_baseline(args.get("label"))
        elif name == "geiter_compare":
            value = store.compare(args.get("baseline_id"))
        elif name == "geiter_propose_experiment":
            value = store.propose_experiment(args["hypothesis"], args["change"], args.get("risk", "low"))
        elif name == "geiter_record_experiment_result":
            value = store.record_experiment_result(
                args["experiment_id"], args["outcome"], args["evidence"], args.get("baseline_id")
            )
        elif name == "geiter_iterate":
            value = store.iterate(args.get("hypothesis"))
        else:
            return _error(request_id, -32602, f"unknown tool: {name}")
    except (KeyError, ValueError) as exc:
        return _error(request_id, -32602, str(exc))
    return _result(request_id, value)


def serve(root: str = ".") -> None:
    store = GeiterStore(root)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = dispatch(store, request)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as exc:
            print(json.dumps(_error(None, -32700, str(exc))), flush=True)
