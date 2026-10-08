"""Geiter runs responsibilities.

    Provider run ledgers, bounded retries, and resumption support.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import synchronized, now


class RunsMixin:

    def start_run(
        self,
        provider: str,
        prompt_ids: list[str],
        target: str | None = None,
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        run = self.add(
            "runs",
            "provider.run",
            {
                "provider": provider,
                "prompt_ids": prompt_ids,
                "target": target,
                "status": "running",
                "max_attempts": max_attempts,
                "attempts": [],
            },
        )
        self.event("runs.started", run)
        return run

    @synchronized
    def record_run_attempt(
        self,
        run_id: str,
        prompt_id: str,
        status: str,
        observation_id: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempt = {
            "prompt_id": prompt_id,
            "attempt_number": sum(item["prompt_id"] == prompt_id for item in run["data"]["attempts"]) + 1,
            "status": status,
            "observation_id": observation_id,
            "error": error,
            "error_type": error_type,
            "retryable": retryable,
            "duration_ms": duration_ms,
            "at": now(),
        }
        run["data"]["attempts"].append(attempt)
        self._write(state)
        self.event("runs.attempted", {"run_id": run_id, **attempt})
        return attempt

    @synchronized
    def finish_run(self, run_id: str) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempts = run["data"]["attempts"]
        expected = set(run["data"]["prompt_ids"])
        completed = {item["prompt_id"] for item in attempts if item["status"] == "succeeded"}
        failed = {item["prompt_id"] for item in attempts if item["status"] == "failed"}
        exhausted = {
            prompt_id for prompt_id in expected
            if sum(item["prompt_id"] == prompt_id for item in attempts) >= run["data"]["max_attempts"]
        }
        eligible = set(self.retryable_prompt_ids(run_id))
        run["data"]["status"] = (
            "completed" if expected <= completed
            else "running" if eligible
            else "partial"
        )
        run["data"]["summary"] = {
            "expected": len(expected),
            "succeeded": len(completed),
            "failed": len(failed),
            "pending": len(expected - completed - failed),
            "exhausted": len(exhausted - completed),
        }
        run["data"]["finished_at"] = now()
        self._write(state)
        self.event("runs.finished", {"run_id": run_id, "status": run["data"]["status"]})
        return run

    def retryable_prompt_ids(self, run_id: str) -> list[str]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempts = run["data"]["attempts"]
        prompt_ids = []
        for prompt_id in run["data"]["prompt_ids"]:
            history = [item for item in attempts if item["prompt_id"] == prompt_id]
            if any(item["status"] == "succeeded" for item in history):
                continue
            if history and history[-1].get("retryable") is False:
                continue
            if len(history) < run["data"]["max_attempts"]:
                prompt_ids.append(prompt_id)
        return prompt_ids

    def run(self, run_id: str) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run
