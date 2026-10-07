"""Provider contracts and deterministic adapters.

Real network providers belong in optional integrations. The core ships with
fixtures so an agent can replay and compare observations without credentials.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from importlib.metadata import entry_points
from typing import Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProviderAnswer:
    provider: str
    answer: str
    citations: list[str]


class Provider(Protocol):
    name: str

    def answer(self, prompt: str) -> ProviderAnswer:
        ...


class FixtureProvider:
    name = "fixture"

    def __init__(self, answers: dict[str, ProviderAnswer] | None = None):
        self.answers = answers or {}

    def answer(self, prompt: str) -> ProviderAnswer:
        if prompt in self.answers:
            return self.answers[prompt]
        return ProviderAnswer(
            provider=self.name,
            answer=f"No fixture answer configured for: {prompt}",
            citations=[],
        )


class JsonlProvider:
    """Replay provider answers from JSONL records.

    Each line must contain ``prompt``, ``answer`` and optional ``citations``.
    """

    name = "jsonl"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.answers = self._load()

    def _load(self) -> dict[str, ProviderAnswer]:
        answers: dict[str, ProviderAnswer] = {}
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                prompt = str(item["prompt"])
                answers[prompt] = ProviderAnswer(
                    provider=str(item.get("provider", self.name)),
                    answer=str(item["answer"]),
                    citations=[str(url) for url in item.get("citations", [])],
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid provider fixture at line {line_number}: {exc}") from exc
        return answers

    def answer(self, prompt: str) -> ProviderAnswer:
        return self.answers.get(
            prompt,
            ProviderAnswer(self.name, f"No fixture answer configured for: {prompt}", []),
        )


class HttpJsonProvider:
    """POST prompts to a JSON endpoint and parse the stable provider contract."""

    name = "http_json"

    def __init__(
        self,
        endpoint: str,
        headers: dict[str, str] | None = None,
        timeout: int | float = 30,
    ):
        if not isinstance(endpoint, str):
            raise ValueError("http_json endpoint must be an absolute http(s) URL")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("http_json endpoint must be an absolute http(s) URL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("http_json timeout must be positive")
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.headers = dict(headers or {})
        for key, value in self.headers.items():
            if not isinstance(key, str) or not key.strip() or "\r" in key or "\n" in key:
                raise ValueError("http_json header names must be non-empty and newline-free")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ValueError("http_json header values must be newline-free strings")

    def answer(self, prompt: str) -> ProviderAnswer:
        request = Request(
            self.endpoint,
            data=json.dumps({"prompt": prompt}).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self.headers,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code in {408, 425, 429} or exc.code >= 500:
                raise ConnectionError(
                    f"http_json provider returned retryable status {exc.code}"
                ) from exc
            raise ValueError(f"http_json provider returned status {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"http_json provider request failed: {exc}") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("http_json provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("http_json provider response must be a JSON object")
        answer = payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("http_json provider response requires a non-empty answer")
        citations = payload.get("citations", [])
        if citations is None:
            citations = []
        if not isinstance(citations, list):
            raise ValueError("http_json provider citations must be an array")
        return ProviderAnswer(
            provider=str(payload.get("provider") or self.name),
            answer=answer,
            citations=[str(citation) for citation in citations],
        )


def observe_prompts(store, prompts: Iterable[dict], provider: Provider, target: str | None = None, run_id: str | None = None) -> list[dict]:
    """Run a provider over prompts and persist successes and failures."""
    results = []
    for prompt in prompts:
        started = time.perf_counter()
        try:
            response = provider.answer(prompt["data"]["text"])
            observation = store.record_observation(
                prompt["id"], response.provider, response.answer, response.citations, target
            )
            results.append(observation)
            if run_id:
                store.record_run_attempt(
                    run_id,
                    prompt["id"],
                    "succeeded",
                    observation["id"],
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
        except Exception as exc:
            if run_id:
                error_type = type(exc).__name__
                retryable = isinstance(exc, (TimeoutError, ConnectionError, OSError))
                store.record_run_attempt(
                    run_id,
                    prompt["id"],
                    "failed",
                    error=f"{error_type}: {exc}",
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    error_type=error_type,
                    retryable=retryable,
                )
    return results


def run_provider(
    store,
    prompts: Iterable[dict],
    provider: Provider,
    target: str | None = None,
    max_attempts: int = 1,
) -> dict:
    prompt_list = list(prompts)
    run = store.start_run(provider.name, [prompt["id"] for prompt in prompt_list], target, max_attempts)
    observations = []
    for attempt_number in range(max_attempts):
        retry_ids = set(store.retryable_prompt_ids(run["id"]))
        if not retry_ids:
            break
        batch = [prompt for prompt in prompt_list if prompt["id"] in retry_ids]
        observations.extend(observe_prompts(store, batch, provider, target, run["id"]))
    return {
        "run": store.finish_run(run["id"]),
        "observations": observations,
    }


def resume_provider(store, run_id: str, provider: Provider) -> dict:
    """Resume a prior run, processing only prompts still eligible for retry."""
    run = store.run(run_id)
    if run["data"]["provider"] != provider.name:
        raise ValueError(
            f"provider mismatch for run {run_id}: expected {run['data']['provider']}, got {provider.name}"
        )
    if run["data"]["status"] == "completed":
        raise ValueError(f"run {run_id} is already completed")
    prompt_map = {prompt["id"]: prompt for prompt in store.prompts()}
    missing = [prompt_id for prompt_id in run["data"]["prompt_ids"] if prompt_id not in prompt_map]
    if missing:
        raise ValueError(f"run {run_id} references missing prompts: {', '.join(missing)}")
    attempted_prompt_ids = store.retryable_prompt_ids(run_id)
    observations = []
    for _ in range(run["data"]["max_attempts"]):
        retry_ids = set(store.retryable_prompt_ids(run_id))
        if not retry_ids:
            break
        batch = [prompt_map[prompt_id] for prompt_id in run["data"]["prompt_ids"] if prompt_id in retry_ids]
        observations.extend(observe_prompts(store, batch, provider, run["data"].get("target"), run_id))
    return {
        "resumed_from": run_id,
        "attempted_prompt_ids": attempted_prompt_ids,
        "run": store.finish_run(run_id),
        "observations": observations,
    }


def regression_run(store, prompts: Iterable[dict], provider: Provider, target: str | None = None) -> dict:
    """Run a provider batch and return its machine-readable quality gate."""
    result = run_provider(store, prompts, provider, target, max_attempts=1)
    return {
        **result,
        "gate": store.regression_gate(result["run"]),
    }


def load_provider(name: str, **kwargs) -> Provider:
    """Load a built-in or installed provider plugin by name."""
    if name == "jsonl":
        return JsonlProvider(kwargs["path"])
    if name == "fixture":
        return FixtureProvider(kwargs.get("answers"))
    if name == "http_json":
        return HttpJsonProvider(
            kwargs.get("endpoint"),
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout", 30),
        )
    discovered = entry_points()
    matches = discovered.select(group="geiter.providers", name=name)
    if not matches:
        raise ValueError(f"unknown provider: {name}")
    factory = next(iter(matches)).load()
    return factory(**kwargs)
