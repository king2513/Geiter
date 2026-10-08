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
- provider run ledger with per-prompt success/failure attempts
- resumable provider runs that continue from a durable run ID
- structured attempt timing, error classes, and retryability decisions
- stdio JSON-RPC gateway with tools and resources
- bounded agent bootstrap context through CLI, tool, and resource surfaces
- `doctor` consistency checks and `report` snapshots
- replayable JSONL provider runs
- zero-dependency `http_json` provider for opt-in live retrieval
- installable provider plugins through `geiter.providers` entry points
- baseline snapshots and delta comparisons between observation batches
- durable regression gate verdicts available after the original run response
- policy-gated experiment proposals with explicit approval
- experiment result ledger linking evidence, comparisons, and learnings
- direction-safe north-star score with a weakest-dimension pointer
- read-only self-introspection that ranks evidence-backed improvement opportunities
- autonomous execute cycles that apply a change, verify it with the regression gate, and revert it when the evidence rejects it
- a surface policy gate where anything but the sandbox defaults to requiring human approval
- a durable approval workflow so governed changes wait for a named approver before touching a real surface
- a compounding playbook that learns which change strategies actually improve the score
- evidence-aware `iterate` cycles that consume comparisons and pending experiments
- persistent prioritized agent action queue with evidence-backed completion
- agent-proposed actions with deduplication and explicit priorities
- lease-backed action recovery for interrupted agent work
- provider/intent coverage matrix with weakest-cell recommendations
- zero runtime dependencies; Python 3.10+

## Quick start

Install from the repository:

```bash
python -m pip install .
```

```bash
python -m geiter init
python -m geiter goal add "Make Geiter easy for agents to discover and trust"
python -m geiter prompt add "What is Geiter?" --intent discovery
python -m geiter prompt list --json
python -m geiter observe <prompt-id> --provider fixture --answer "Geiter is an agent-native GEO runtime." --citation https://example.com/geiter
python -m geiter analyze --json
python -m geiter score --json
python -m geiter introspect --json
python -m geiter health --json
python -m geiter matrix --json
python -m geiter doctor --json
python -m geiter report --json
python -m geiter context --json
python -m geiter baseline save --label before-change --json
python -m geiter baseline compare --json
python -m geiter experiment propose --hypothesis "Improve citation rate" --change "Add authoritative docs" --json
python -m geiter experiment result --id <experiment-id> --outcome supported --evidence "Citation rate improved" --json
python -m geiter iterate --json
python -m geiter execute --heading "Geiter: overview" --body "Geiter is an agent-native GEO runtime." --json
python -m geiter govern propose --surface-id my-docs --heading "Overview" --body "..." --json
python -m geiter govern approve <request-id> --approver alice --json
python -m geiter govern reject <request-id> --approver alice --json
python -m geiter govern list --json
python -m geiter experience --json
python -m geiter action propose --type investigate --prompt "Check the weakest citation cell" --priority high --json
python -m geiter action list --json
python -m geiter action list --status stale --json
python -m geiter action complete <action-id> --evidence "Verified the repair" --json
python -m geiter action skip <action-id> --evidence "Superseded by a newer experiment" --json
python -m geiter action reclaim <action-id> --evidence "Previous agent stopped responding" --json
```

Replay a deterministic provider batch:

```bash
python -m geiter prompt add "What is Geiter?" --intent discovery
python -m geiter run --provider jsonl --fixture examples/answers.jsonl --json
python -m geiter regression --provider jsonl --fixture examples/answers.jsonl --json
# Compare this run with an explicit baseline.
python -m geiter regression --provider jsonl --fixture examples/answers.jsonl \
  --baseline-id <baseline-id> --json
# Continue a run after a process interruption.
python -m geiter resume <run-id> --provider jsonl --fixture examples/answers.jsonl --json
```

For an explicitly configured live JSON provider, the request is a POST body
`{"prompt":"..."}` and the response must contain `{"answer":"...","citations":[]}`:

```bash
python -m geiter run --provider http_json \
  --endpoint https://provider.example/answer \
  --header "Authorization: Bearer $GEITER_PROVIDER_TOKEN" \
  --timeout 30 --json
```

Use `--root` to point Geiter at another workspace:

```bash
python -m geiter --root ./demo status --json
```

Print a client-ready MCP configuration:

```bash
python -m geiter connect --format generic --json
python -m geiter connect --format claude --json
python -m geiter connect --format cursor --json
python -m geiter connect --format vscode --json
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

Tools include `geiter_status`, `geiter_context`, `geiter_add_prompt`, `geiter_record_observation`, `geiter_analyze`, `geiter_score`, `geiter_introspect`, `geiter_experience`, `geiter_execute`, `geiter_govern`, `geiter_approvals`, `geiter_health`, `geiter_runs`, `geiter_report`, `geiter_resume`, `geiter_regression`, `geiter_matrix`, `geiter_save_baseline`, `geiter_compare`, `geiter_propose_experiment`, `geiter_record_experiment_result`, `geiter_iterate`, `geiter_actions`, `geiter_propose_action`, `geiter_complete_action`, `geiter_skip_action`, `geiter_reclaim_action`, `geiter_connect`, and `geiter_capabilities`. Resources include `geiter://status`, `geiter://context`, `geiter://report`, `geiter://score`, `geiter://introspect`, `geiter://experience`, and `geiter://capabilities`.

An agent can call `geiter_context` or read `geiter://context` once at startup
to receive capabilities, workspace status, quality checks, queued work, and a
bounded next-action decision without mutating state.

To resume a provider run from an agent client:

```json
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"geiter_resume","arguments":{"run_id":"run_...","provider":"jsonl","fixture":"examples/answers.jsonl"}}}
```

## Provider plugins

The built-in `http_json` adapter performs no network call unless an agent
explicitly selects it and supplies an endpoint. Third-party packages can also
register a provider without changing Geiter:

```toml
[project.entry-points."geiter.providers"]
my-provider = "my_package.provider:factory"
```

The factory returns an object implementing:

```python
answer(prompt: str) -> ProviderAnswer
```

The built-in JSONL adapter is replay-only and has no network access, which keeps
regression runs deterministic and reviewable.
Provider credentials are supplied at runtime and are not written into the
state or event ledger.

`regression` runs a fixture against all configured prompts and returns a
`geiter/gate-v1` object with an `ok` boolean, named checks, health diagnostics,
the run ID, a baseline-scoped comparison, and a typed `action` for the next
step. Pass `--baseline-id` to select a baseline; when omitted, the newest
baseline is used if one exists. The gate keeps the current run's observations
separate from other post-baseline runs. `improved` and `flat` comparisons pass;
`regressed`, `mixed`, and `insufficient_data` produce a failing gate with an
action that tells the next agent what evidence or repair is required. Without
a baseline, the original run-quality gate remains compatible and reports
`comparison.status = no_baseline`. Agents and CI can use this as a single
machine-readable contract without interpreting prose.

The gate summary is persisted on the provider run at
`runs[].data.gate`. `report --json` exposes the newest one as
`latest_gate`; `context --json` exposes a bounded form as
`decision.latest_gate`. Supplying an unknown `--baseline-id` fails explicitly
with `comparison.status = unknown_baseline` and a `select_valid_baseline`
action instead of silently selecting another baseline.

`geiter://capabilities` is the stable discovery surface for agents: it reports
the version, state and report schemas, transport, entrypoints, and operating
principles without changing workspace state.

Regression gate actions are persisted in the workspace. `iterate` claims the
highest-priority open action without pretending to execute external work.
Agents can propose work with `action propose`, inspect it by priority with
`action list`, then complete or skip it with evidence using `action complete` or
`action skip`.
Action completion and skip operations require at least one non-empty scalar
evidence value; empty or nested-only evidence is rejected. If a failed gate has
no open action left, `iterate` keeps that gate as its next decision signal
instead of silently returning to general workspace advice.
Reports distinguish open work from in-progress work so claimed actions remain
visible across processes. Claims carry a one-hour lease by default. Interrupted
claims appear under `action list --status stale` and can be requeued with
`action reclaim`; reclaiming is idempotent and never changes completed or skipped
actions. A normal `iterate` cycle also reclaims expired work before selecting
its next action.
An active action claim is owner-specific: repeating a claim from the same
iteration is idempotent, while another agent receives a claim conflict instead
of being told it owns the work.

Every batch run creates a durable run ledger. Provider exceptions are recorded
per prompt, successful observations are preserved, and the batch finishes as
`completed` or `partial` instead of silently losing the failure.
Pass `--max-attempts N` to retry only failed prompts up to `N` total attempts;
successful prompts are never replayed. A run with remaining retryable prompts
can be continued later with `resume <run-id>`; the provider and fixture are
supplied again so the ledger remains portable and auditable.

## Self-assessment

Geiter can answer two questions an agent needs before choosing its next move:

- `score` (`geiter/score-v1`) aggregates the retrieval signals into one
  direction-safe north-star value between 0 and 100, together with per-dimension
  detail and a `weakest_dimension` pointer. Higher is always better, so a rising
  score means the knowledge surface is easier to discover and attribute. When the
  usable sample is too small the score reports `insufficient_data` instead of
  inventing a number.
- `introspect` (`geiter/introspect-v1`) is read-only and ranks the most valuable
  improvement opportunities by combining coverage gaps, observation health, and the
  weakest coverage-matrix cells, each with the evidence that produced it.

Both are read-only, are exposed through the CLI, the gateway, and MCP-style
resources, and surface a bounded form in the agent bootstrap context.

## Autonomous execution

Geiter can act on its own knowledge instead of only measuring and proposing.
`execute` runs one verified change cycle against a local sandbox surface:

```text
introspect -> propose change -> snapshot -> apply -> re-observe -> gate
           -> accept (keep the change) or revert (roll it back)
```

The regression gate is the arbiter. A change is kept only when the evidence
gate accepts it; if the gate rejects the change, the surface is restored from the
snapshot taken before the change and the outcome is recorded as a learning. This
means an agent may act autonomously while still being unable to keep an action
that its own evidence rejects.

The surface is a plain local directory (`.geiter/../sandbox`) and a
surface-aware provider answers from its live content, so the gate judges the real
effect of a change rather than a canned response. Nothing in this loop touches
external or real content.

## Change governance

Autonomy needs a boundary. Every surface other than the trusted sandbox declares
a policy that decides whether a change may be applied:

| Level | Behavior |
| --- | --- |
| `auto` | Apply immediately, then let the regression gate accept or revert. |
| `approve` | Create a durable approval request and wait for a named approver. This is the default for any undeclared surface. |
| `deny` | Refuse the change without touching the surface. |

A governed change persists as an approval request, so it survives across
processes and can be approved or rejected by something other than the proposing
agent:

```bash
python -m geiter govern propose --surface-id my-docs --heading "Overview" --body "..."
python -m geiter govern approve <request-id> --approver alice
python -m geiter govern list
```

Approval authorizes the **attempt**, never the **outcome**: an approved change
still has to survive the regression gate, so no surface can be mutated by a
change that the evidence rejects.

## Compounding (the self-improvement flywheel)

Every executed change already leaves an `execution.result` learning. The
`experience` surface turns that log into a durable playbook: for each change
kind it reports attempts, accept rate, and the average north-star delta. The
execute cycle then prefers a kind that historically improved the score:

```bash
python -m geiter experience --json
```

A strategy only earns preference after a minimum number of samples, so a single
lucky change cannot lock the loop into one approach, and a cold start falls back
to the default deterministically. This is the compounding part of self-iteration:
the more Geiter iterates, the better it gets at choosing what to try next.

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
