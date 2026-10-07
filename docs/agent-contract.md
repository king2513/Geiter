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
observe <prompt-id> --provider <name> --answer <text> [--citation <url>] --json
analyze --json
health --json
matrix --json
doctor --json
report --json
run --provider jsonl --fixture <path> --json
regression --provider jsonl --fixture <path> --json
resume <run-id> --provider jsonl --fixture <path> --json
connect [--format <generic|claude|cursor|vscode>] --json
baseline save [--label <label>] --json
baseline compare [--id <baseline-id>] --json
experiment propose --hypothesis <text> --change <text> [--risk <level>] --json
experiment approve --id <experiment-id> --json
experiment result --id <experiment-id> --outcome <supported|rejected|inconclusive> --evidence <text> --json
iterate [--hypothesis <text>] --json
action list [--status <open|in_progress|completed|skipped|all>] --json
action complete <action-id> [--evidence <text>] --json
action skip <action-id> [--evidence <text>] --json
iterate [--hypothesis <text>] --json
event list --json
```

For process-based integrations, run `python -m geiter gateway` and send one
JSON-RPC request per line over stdin. Responses are one JSON object per line.
`initialize` advertises both `tools` and `resources` capabilities. Invalid
JSON-RPC envelopes return `-32600`; unexpected server failures return `-32603`.
The gateway currently exposes `geiter_status`, `geiter_add_prompt`,
`geiter_record_observation`, `geiter_analyze`, `geiter_report`, and
`geiter_resume`. The resume tool requires `run_id` and `provider`, plus
`fixture` for the built-in JSONL provider.
`geiter_connect` returns the same configuration shape as the CLI and never
writes client configuration files.
`geiter_capabilities` and `geiter://capabilities` expose the same stable
capability document, including version, schemas, transport, and entrypoints.
`geiter_regression` runs the configured prompts against a provider and returns
a `geiter/gate-v1` payload containing an `ok` boolean, named checks, failed
check names, and a typed next-step `action`.
The gate action is also persisted as an `agent.action` record. `iterate`
claims the highest-priority open action and records it in the iteration trace;
claiming does not imply that external work was completed.
`geiter_actions` lists queued actions by priority; `geiter_complete_action`
or `geiter_skip_action` resolves one with optional evidence. Open actions with
the same dedupe key are idempotent. Status and report payloads expose both
`open_action_count` and `in_progress_action_count`.
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

The built-in JSONL adapter is replay-only and has no network access. This makes
regression runs safe to commit as fixtures and compare across Geiter versions.

Baselines snapshot the current analysis. A comparison analyzes only observations
recorded after the selected baseline, so repeated observations cannot masquerade
as improvement. Comparisons pair observations by prompt and provider, and only
emit a directional verdict after at least two pairs; otherwise the verdict is
`insufficient_data`. Experiments are proposals by default; approval only changes the
local record and refuses experiments marked with external side effects.
An approved experiment can be completed with a structured result, which writes
an `experiment.result` learning containing the selected comparison and evidence.
`iterate` includes the latest comparison context, pending approved experiments,
the claimed action (when present), and a next prompt chosen from those signals.
`matrix` groups observations by provider and prompt intent, then returns the
weakest cells an agent should improve first.

Installed provider plugins are discovered through the `geiter.providers` Python
entry-point group. A plugin factory must return an object with:

```python
answer(prompt: str) -> ProviderAnswer
```

Use `target_citation_rate` for source attribution. `citation_rate` alone only
means the provider returned some citation.
