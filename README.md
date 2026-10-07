# Geiter

[![CI](https://github.com/king2513/Geiter/actions/workflows/ci.yml/badge.svg)](https://github.com/king2513/Geiter/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/king2513/Geiter?display_name=tag)](https://github.com/king2513/Geiter/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)

> The self-improving GEO runtime for agents.

Geiter is an agent-native runtime for Generative Engine Optimization (GEO). It gives agents a durable loop to inspect a knowledge surface, probe retrieval, measure mention and citation signals, and choose the next experiment.

**Geiter is built for agents first.** Humans can read the files, but the primary interface is typed state, stable JSON, JSON-RPC tools, and replayable evidence.

## What ships today

- durable local state in `.geiter/state.json`
- append-only event log in `.geiter/events.jsonl`
- deterministic retrieval prompts and provider observations
- mention, citation, target-citation, and citation-position signals
- direction-safe reciprocal-rank citation signal and paired comparisons
- observation/provider health checks that filter unusable evidence
- stdio JSON-RPC gateway with tools and resources
- `doctor` consistency checks and `report` snapshots
- replayable JSONL provider runs
- installable provider plugins through `geiter.providers` entry points
- baseline snapshots and delta comparisons between observation batches
- policy-gated experiment proposals with explicit approval
- experiment result ledger linking evidence, comparisons, and learnings
- evidence-aware `iterate` cycles that consume comparisons and pending experiments
- provider/intent coverage matrix with weakest-cell recommendations
- zero runtime dependencies; Python 3.10+

## Quick start

```bash
python -m geiter init
python -m geiter goal add "Make Geiter easy for agents to discover and trust"
python -m geiter prompt add "What is Geiter?" --intent discovery
python -m geiter prompt list --json
python -m geiter observe <prompt-id> --provider fixture --answer "Geiter is an agent-native GEO runtime." --citation https://example.com/geiter
python -m geiter analyze --json
python -m geiter health --json
python -m geiter matrix --json
python -m geiter doctor --json
python -m geiter report --json
python -m geiter baseline save --label before-change --json
python -m geiter baseline compare --json
python -m geiter experiment propose --hypothesis "Improve citation rate" --change "Add authoritative docs" --json
python -m geiter experiment result --id <experiment-id> --outcome supported --evidence "Citation rate improved" --json
python -m geiter iterate --json
```

Replay a deterministic provider batch:

```bash
python -m geiter run --provider jsonl --fixture examples/answers.jsonl --json
```

Use `--root` to point Geiter at another workspace:

```bash
python -m geiter --root ./demo status --json
```

## Core model

```text
Workspace
  |- identity       what Geiter is trying to become
  |- goals          desired outcomes
  |- memories       durable principles and facts
  |- prompts        retrieval questions to probe
  |- observations   what the agent currently sees
  |- hypotheses     explanations worth testing
  |- actions        changes or experiments
  |- measurements   evidence of effect
  `- learnings      what should change in the next loop
```

The runtime does not pretend to be a full crawler, search engine, or LLM. Those capabilities attach through adapters without changing the core contract.

## Agent gateway

Run the stdio JSON-RPC gateway:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python -m geiter gateway
```

Tools include `geiter_status`, `geiter_add_prompt`, `geiter_record_observation`, `geiter_analyze`, and `geiter_report`. Resources include `geiter://status` and `geiter://report`.

## Provider plugins

Third-party packages can register a provider without changing Geiter:

```toml
[project.entry-points."geiter.providers"]
my-provider = "my_package.provider:factory"
```

The factory returns an object implementing:

```python
answer(prompt: str) -> ProviderAnswer
```

The built-in JSONL adapter is replay-only and has no network access, which keeps regression runs deterministic and reviewable.

## Design principles

- **Evidence is first-class.** Every observation keeps provider, answer hash, citations, metrics, and event history.
- **Replay before reach.** Fixtures make experiments deterministic before connecting a live provider.
- **Adapters stay outside the kernel.** Provider plugins extend Geiter without forcing dependencies into every installation.
- **Autonomy has a boundary.** Geiter measures and proposes; external publishing needs an explicit adapter and policy.

## Development

```bash
python -m unittest discover -s tests -v
python tests/syntax_check.py
python -m geiter --help
```

See [docs/agent-contract.md](docs/agent-contract.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [CHANGELOG.md](CHANGELOG.md) for the machine contract and release history.

## Next frontier

The active project frontier is live provider adapters, richer retrieval evaluators, policy-gated experiments, and a SQLite backend when query volume outgrows the local JSON contract.
