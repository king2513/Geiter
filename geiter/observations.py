"""Geiter observations responsibilities.

    Retrieval prompts and provider observations with GEO metrics.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from .core import Record, now, synchronized, uid


class ObservationsMixin:

    @synchronized
    def add_prompt(self, text: str, intent: str | None = None) -> dict[str, Any]:
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("prompt text must not be empty")
        prompt_id = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:16]
        state = self.read()
        existing = next((item for item in state["prompts"] if item["id"] == prompt_id), None)
        if existing:
            return existing
        record = {
            "id": prompt_id,
            "created_at": now(),
            "kind": "prompt",
            "data": {"text": normalized, "intent": intent},
        }
        state["prompts"].append(record)
        self._write(state)
        self.event("prompts.added", record)
        return record

    @synchronized
    def record_observation(
        self,
        prompt_id: str,
        provider: str,
        answer: str,
        citations: list[str] | None = None,
        target: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        prompt = next((item for item in state["prompts"] if item["id"] == prompt_id), None)
        if prompt is None:
            raise ValueError(f"unknown prompt id: {prompt_id}")
        if run_id is not None:
            run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
            if run is None:
                raise ValueError(f"unknown run id: {run_id}")
            if prompt_id not in run["data"].get("prompt_ids", []):
                raise ValueError(f"prompt {prompt_id} is not part of run {run_id}")
        clean_citations = list(dict.fromkeys(citations or []))
        normalized_answer = answer.strip()
        answer_sha256 = hashlib.sha256(normalized_answer.encode("utf-8")).hexdigest()
        invalid_citations = [citation for citation in clean_citations if not urlparse(citation).scheme]
        quality = {
            "answer_present": bool(normalized_answer),
            "citations_valid": not invalid_citations,
            "duplicate": any(
                item["data"].get("prompt_id") == prompt_id
                and item["data"].get("provider") == provider
                and item["data"].get("answer_sha256") == answer_sha256
                and (
                    item["data"].get("run_id") == run_id
                    if run_id is not None
                    else item["data"].get("run_id") is None
                )
                for item in state.get("observations", [])
                if item.get("kind") == "retrieval.observation"
            ),
        }
        quality["usable"] = quality["answer_present"] and quality["citations_valid"] and not quality["duplicate"]
        target_text = (target or state["identity"]["name"]).casefold()
        mention = target_text in normalized_answer.casefold()
        target_tokens = [token for token in target_text.replace("-", " ").split() if token]
        positions = []
        for index, citation in enumerate(clean_citations):
            parsed = urlparse(citation)
            source_tokens = [
                token
                for chunk in (parsed.netloc, parsed.path)
                for token in chunk.casefold().replace("-", "/").replace("_", "/").replace(".", "/").split("/")
                if token
            ]
            if (
                target_tokens
                and all(token in source_tokens for token in target_tokens)
                and parsed.path != f"/{target_tokens[0]}"
            ):
                positions.append(index)
            elif (
                len(target_tokens) == 1
                and len(target_tokens[0]) <= 3
                and target_tokens[0] in source_tokens
                and target_text in parsed.netloc.casefold()
            ):
                positions.append(index)
        observation_data = {
            "prompt_id": prompt_id,
            "prompt": prompt["data"]["text"],
            "provider": provider,
            "answer": normalized_answer,
            "answer_sha256": answer_sha256,
            "citations": clean_citations,
            "quality": quality,
            "target": target or state["identity"]["name"],
            "metrics": {
                "mention": int(mention),
                "citation": int(bool(clean_citations)),
                "target_citation": int(bool(positions)),
                "citation_position": positions[0] + 1 if positions else None,
                "citation_reciprocal_rank": round(1 / (positions[0] + 1), 4) if positions else 0.0,
                "citation_count": len(clean_citations),
            },
        }
        if run_id is not None:
            observation_data["run_id"] = run_id
        record = Record(
            uid("obs"),
            now(),
            "retrieval.observation",
            observation_data,
        ).to_dict()
        state["observations"].append(record)
        state["measurements"].append({
            "id": uid("met"),
            "created_at": record["created_at"],
            "kind": "retrieval.metrics",
            "data": {"observation_id": record["id"], **record["data"]["metrics"]},
        })
        self._write(state)
        self.event("observations.recorded", record)
        return record
