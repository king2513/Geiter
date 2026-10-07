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
baseline save [--label <label>] --json
baseline compare [--id <baseline-id>] --json
experiment propose --hypothesis <text> --change <text> [--risk <level>] --json
experiment approve --id <experiment-id> --json
experiment result --id <experiment-id> --outcome <supported|rejected|inconclusive> --evidence <text> --json
iterate [--hypothesis <text>] --json
iterate [--hypothesis <text>] --json
event list --json
```

For process-based integrations, run `python -m geiter gateway` and send one
JSON-RPC request per line over stdin. Responses are one JSON object per line.
The gateway currently exposes `geiter_status`, `geiter_add_prompt`,
`geiter_record_observation`, `geiter_analyze`, and `geiter_report`.
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
`completed` only when every prompt succeeds; otherwise it is `partial`.
`--max-attempts` bounds retries. `summary.exhausted` counts prompts that still
failed after reaching that bound.
`geiter_runs` exposes recent ledgers to agent callers.

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
and a next prompt chosen from those signals.
`matrix` groups observations by provider and prompt intent, then returns the
weakest cells an agent should improve first.

Installed provider plugins are discovered through the `geiter.providers` Python
entry-point group. A plugin factory must return an object with:

```python
answer(prompt: str) -> ProviderAnswer
```

Use `target_citation_rate` for source attribution. `citation_rate` alone only
means the provider returned some citation.
