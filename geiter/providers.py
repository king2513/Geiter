"""Provider contracts and deterministic adapters.

Real network providers belong in optional integrations. The core ships with
fixtures so an agent can replay and compare observations without credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


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


def observe_prompts(store, prompts: Iterable[dict], provider: Provider, target: str | None = None) -> list[dict]:
    """Run a provider over prompts and persist every observation."""
    results = []
    for prompt in prompts:
        response = provider.answer(prompt["data"]["text"])
        results.append(store.record_observation(
            prompt["id"],
            response.provider,
            response.answer,
            response.citations,
            target,
        ))
    return results
