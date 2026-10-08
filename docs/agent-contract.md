# Agent Contract

Geiter's public contract is intentionally small. An agent can operate it without
importing Python internals:

```text
init
status --json
goal add <text> --json
memory add <kind> <text> --json
prompt add <text> [--intent <intent>] --json
prompt list --json
observe <prompt-id> --provider <name> --answer <text> [--citation <url>] [--run-id <run-id>] --json
analyze --json
score --json
introspect --json
health --json
matrix --json
doctor --json
report --json
context --json
run --provider jsonl --fixture <path> --json
run --provider http_json --endpoint <url> [--header "Name: value"] [--timeout <seconds>] --json
regression --provider jsonl --fixture <path> [--baseline-id <baseline-id>] --json
regression --provider http_json --endpoint <url> [--header "Name: value"] [--timeout <seconds>] [--baseline-id <baseline-id>] --json
resume <run-id> --provider jsonl --fixture <path> --json
resume <run-id> --provider http_json --endpoint <url> [--header "Name: value"] [--timeout <seconds>] --json
connect [--format <generic|claude|cursor|vscode>] --json
baseline save [--label <label>] --json
baseline compare [--id <baseline-id>] --json
experiment propose --hypothesis <text> --change <text> [--risk <level>] --json
experiment approve --id <experiment-id> --json
experiment result --id <experiment-id> --outcome <supported|rejected|inconclusive> --evidence <text> --json
iterate [--hypothesis <text>] --json
action list [--status <open|in_progress|completed|skipped|stale|all>] --json
action propose --type <type> --prompt <text> [--priority <normal|high|critical>] [--source <source>] [--dedupe-key <key>] --json
action complete <action-id> --evidence <text> --json
action skip <action-id> --evidence <text> --json
action reclaim <action-id> [--evidence <text>] --json
event list --json
```

For process-based integrations, run `python -m geiter gateway` and send one
JSON-RPC request per line over stdin. Responses are one JSON object per line.
`initialize` advertises both `tools` and `resources` capabilities. Invalid
JSON-RPC envelopes return `-32600`; unexpected server failures return `-32603`.
The gateway exposes the same contract through typed tools for status, prompts,
observations, analysis, health, runs, capabilities, actions, connections,
baselines, experiments, iteration, regression, and resumable provider runs.
`tools/list` is the authoritative discovery surface; the action tools include
`geiter_actions`, `geiter_propose_action`, `geiter_complete_action`,
`geiter_skip_action`, and `geiter_reclaim_action`.
`geiter_connect` returns the same configuration shape as the CLI and never
writes client configuration files.
`geiter_context` and `geiter://context` expose the same bounded, read-only
bootstrap packet through a tool and resource. It contains capabilities, status,
quality checks, queued actions, and the next-action decision; it does not append
events or mutate workspace state.
`geiter_capabilities` and `geiter://capabilities` expose the same stable
capability document, including version, schemas, transport, and entrypoints.
`geiter_regression` runs the configured prompts against a provider and returns
a `geiter/gate-v1` payload containing an `ok` boolean, named checks, failed
check names, a baseline-scoped `comparison`, and a typed next-step `action`.
Its optional `baseline_id` selects the comparison baseline; if omitted, the
newest baseline is used when available. Observations created by the run carry
its `run_id`, so the gate never mixes another post-baseline run into the
current comparison. `improved` and `flat` verdicts pass. `regressed`,
`mixed`, and `insufficient_data` fail with typed actions; `no_baseline` keeps
the pre-1.18 run-quality behavior and is explicitly reported.
An unknown `baseline_id` is reported as `comparison.status =
unknown_baseline` and never falls back to another baseline. The compact gate
summary is persisted at `runs[].data.gate`, surfaced as `latest_gate` in
`report`, and exposed as `decision.latest_gate` in the bounded context packet.
The gate action is also persisted as an `agent.action` record. `iterate`
claims the highest-priority open action and records it in the iteration trace;
claiming does not imply that external work was completed.
`geiter_actions` lists queued actions by priority; `geiter_propose_action`
persists a new action without executing external effects; and
`geiter_complete_action` or `geiter_skip_action` resolves one with required
evidence. Resolution requires at least one non-empty scalar evidence value;
empty or nested-only evidence is rejected. Open actions with the same dedupe key are idempotent. Status and
report payloads expose both
`open_action_count` and `in_progress_action_count`, plus stale action counts.
Claims carry a one-hour lease by default. `geiter_actions` accepts the virtual
`stale` status, and `geiter_reclaim_action` requeues an expired claim to `open`.
Reclaiming is idempotent, preserves the prior claim metadata, and refuses active
leases. An active claim is idempotent only for its original claimant; a
different claimant receives an explicit conflict and cannot treat the action as
its own. `geiter_iterate` also reclaims stale actions before claiming the next
priority action, and records claim conflicts in its iteration result.
It also exposes `geiter://status` and `geiter://report` resources through
`resources/list` and `resources/read`.

## Stability rules

- JSON objects use explicit keys and are UTF-8 encoded.
- IDs are stable for prompts and unique for events and records.
- Prompt IDs are derived from normalized prompt text, so adding the same prompt
  twice is idempotent.
- Provider output is stored with an answer hash. This keeps comparisons possible
  without requiring the raw provider response to be trusted as identity.
- `analyze` is observational. It never publishes, mutates external content, or
  calls a provider by itself.

## Retrieval metrics

Each retrieval observation can contain:

- `mention`: target appears in the answer
- `citation`: the answer contains at least one citation
- `target_citation`: a citation resolves to a target token
- `citation_position`: first target citation position, when available
- `citation_reciprocal_rank`: direction-safe citation rank (`1 / position`)

`health` reports provider-level usable rates and flags empty answers, invalid
citation URLs, and duplicate prompt/provider/answer observations. Unusable
observations remain in the audit log but are excluded from GEO aggregates.

`run` returns a `provider.run` record with one attempt per prompt. Attempts have
`succeeded` or `failed` status and preserve the error text. A run is
`completed` only when every prompt succeeds. A run is `running` when retryable
prompts remain within the attempt budget, and `partial` when no more progress
is available.
`--max-attempts` bounds retries. `summary.exhausted` counts prompts that still
failed after reaching that bound.
Each attempt also exposes `attempt_number`, `duration_ms`, `error_type`, and
`retryable`. Timeout, connection, and OS failures are retryable by default;
configuration/value failures are recorded without blind retries.
`geiter_runs` exposes recent ledgers to agent callers.
`resume <run-id>` continues only retryable prompts from that ledger and never
replays a prompt that already succeeded. Resuming an already completed run is
an explicit error.

Provider adapters implement one small contract:

```text
answer(prompt) -> { provider, answer, citations[] }
```

The built-in JSONL adapter is replay-only and has no network access. The
optional `http_json` adapter sends `POST {"prompt":"..."}` to an explicitly
configured `http(s)` endpoint and expects a JSON object with a non-empty
`answer` and optional `citations` array. HTTP 408, 425, 429, 5xx, connection,
and timeout failures are recorded as retryable; other HTTP statuses and invalid
payloads are non-retryable provider errors. Credentials are passed at runtime
and are never written to the Geiter state ledger. Replay runs remain safe to
commit as fixtures and compare across Geiter versions.

Baselines snapshot the current analysis. A comparison analyzes only observations
recorded after the selected baseline, so repeated observations cannot masquerade
as improvement. Comparisons pair observations by prompt and provider, and only
emit a directional verdict after at least two valid pairs; otherwise the verdict
is `insufficient_data`. A regression gate scopes those pairs to its current run,
and accepts only `improved` or `flat`. Experiments are proposals by default; approval only changes the
local record and refuses experiments marked with external side effects.
An approved experiment can be completed with a structured result, which writes
an `experiment.result` learning containing the selected comparison and evidence.
`iterate` includes the latest comparison and regression-gate context, pending
approved experiments, the claimed action (when present), and a next prompt
chosen from those signals. If a failed gate's action was resolved or removed,
the failed gate remains a first-class decision signal in the next iteration.
`matrix` groups observations by provider and prompt intent, then returns the
weakest cells an agent should improve first.

Installed provider plugins are discovered through the `geiter.providers` Python
entry-point group. A plugin factory must return an object with:

```python
answer(prompt: str) -> ProviderAnswer
```

Use `target_citation_rate` for source attribution. `citation_rate` alone only
means the provider returned some citation.

## Concurrency

Multiple agents or processes may share one workspace. Every state-mutating
operation runs while holding a reentrant advisory lock on a sidecar
`.geiter/state.lock` file, so a full read-modify-write cycle is atomic with
respect to other processes and no update is lost. The lock is reentrant, so
nested calls such as `iterate` -> `inspect` -> `add` are safe. If the lock
cannot be acquired within the timeout, the operation fails loudly with a
`TimeoutError` instead of corrupting the state file. Credentials and provider
configuration are never written into the locked state.

## Self-assessment

`score` returns a `geiter/score-v1` object. `north_star` is a single 0-100
value where higher is always better, so it can be compared across runs. It is a
weighted blend of four dimensions: `mention` (0.25), `target_citation` (0.35),
`citation_rank` (0.20, the direction-safe reciprocal rank), and `coverage` (0.20,
the share of configured prompts with at least one usable observation). Each
dimension is reported with its own value, weight, and sample size. When the
usable sample is smaller than `minimum_sample`, the score reports
`status = insufficient_data` and no `north_star` value rather than presenting a
misleading total. `weakest_dimension` names the lowest-scoring dimension that has
enough data to be meaningful.

`introspect` returns a `geiter/introspect-v1` object. It is read-only and ranks
`opportunities` by priority from three evidence sources: unobserved configured
prompts, unusable observations that are excluded from aggregates, and
coverage-matrix cells that are mentioned but never attributed a citation. Each
opportunity carries the evidence that produced it. This is the surface an agent
should read to decide which single change to make next.

Both surfaces are available as the CLI commands `score` and `introspect`, the
gateway tools `geiter_score` and `geiter_introspect`, and the resources
`geiter://score` and `geiter://introspect`. A bounded form is included in
`report` and in the agent bootstrap context as `self_assessment`.
