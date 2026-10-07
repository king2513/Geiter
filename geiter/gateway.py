"""A tiny stdio JSON-RPC gateway for agent and MCP-style callers.

This is intentionally transport-only. The domain contract stays in GeiterStore,
so an MCP server or HTTP adapter can be added later without duplicating logic.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .core import GeiterStore


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
        "name": "geiter_report",
        "description": "Return a complete machine-readable GEO report.",
        "inputSchema": {"type": "object", "properties": {}},
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
            "serverInfo": {"name": "geiter", "version": "0.3.0"},
            "capabilities": {"tools": {}},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
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
        elif name == "geiter_report":
            value = store.report()
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
