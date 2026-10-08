# Changelog

## 1.26.1 - 2026-10-08

- split the store into focused modules (`persistence`, `observations`, `runs`, `actions`, `analysis`, `baselines`, `experiments`, `gate`, `iteration`, `surfaces`) composed as mixins, reducing `core.py` from 1957 to 176 lines
- kept `geiter.core.GeiterStore` as the single public entry point with an unchanged 55-method public API
- reorganized imports so the dependency direction stays explicit and the store remains the only composition root

## 1.26.0 - 2026-10-08

- added an effectiveness aggregator that turns past `execution.result` learnings into a durable playbook of which change kinds actually work
- made the execute cycle choose its strategy from historical effectiveness, so a change kind that improved the score is preferred on later cycles
- required a minimum sample before a strategy earns preference, so one lucky change cannot lock the loop into a single strategy
- added the `experience` surface (`geiter/experience-v1`) with per-kind accept rate, mean score delta, and the recommended next strategy
- exposed `experience` through the CLI, the `geiter_experience` gateway tool, and the `geiter://experience` resource, and surfaced a bounded form in `report` and the agent bootstrap context

## 1.25.0 - 2026-10-08

- added a surface policy gate with `auto`, `approve`, and `deny` levels; every surface other than the trusted sandbox defaults to requiring human approval
- added a durable approval workflow where a governed change becomes a `pending_approval` request that persists across processes until a named approver approves or rejects it
- made approval authorize only the attempt, never the outcome, because the regression gate still accepts or reverts the applied change
- added `execute_governed` so autonomous execution can target a declared real surface without ever mutating it implicitly
- exposed `govern propose|approve|reject|list` through the CLI and the `geiter_govern` and `geiter_approvals` gateway tools, and declared the `geiter/approvals-v1` contract in capabilities metadata

## 1.24.0 - 2026-10-08

- added autonomous execution: a propose/apply/verify cycle that lets Geiter act on its own knowledge surface instead of only measuring and proposing
- added a local sandbox surface with per-change snapshots so any applied change is fully reversible
- made the regression gate the arbiter: a change is kept only when the evidence gate accepts it, and is automatically reverted when it does not
- added a surface-aware provider that answers from the live surface, so the gate judges a change's real effect rather than a canned response
- exposed `execute` through the CLI and the `geiter_execute` gateway tool, and declared the `geiter/execution-v1` contract in capabilities metadata

## 1.23.0 - 2026-10-08

- added a reentrant cross-process file lock around every state-mutating operation to prevent lost updates when multiple agents share a workspace
- used platform-native advisory locking (`fcntl.flock` / `msvcrt.locking`) with a sidecar `.geiter/state.lock` file, keeping the runtime dependency-free
- made nested store calls safe so operations such as `iterate` -> `inspect` -> `add` re-enter the lock instead of deadlocking
- added deterministic tests proving mutating methods hold the lock and that concurrent threads and processes never lose an update

## 1.22.0 - 2026-10-08

- added a direction-safe north-star score (`geiter/score-v1`) that aggregates mention, target citation, citation rank, and coverage into one 0-100 value with a `weakest_dimension`
- added read-only self-introspection (`geiter/introspect-v1`) that ranks evidence-backed improvement opportunities from coverage, health, and matrix gaps
- exposed `score` and `introspect` through the CLI, gateway tools, and resources
- surfaced bounded self-assessment in `report` and the agent bootstrap context
- declared the self-assessment contract in capabilities metadata

## 1.21.0 - 2026-10-08

- made action claims owner-aware and rejected claims by competing agents
- made `iterate` report claim conflicts rather than misrepresenting another agent's action as its own
- documented the action ownership contract in capabilities and agent guidance

## 1.20.0 - 2026-10-08

- made autonomous iteration prioritize unresolved failed regression gates
- recorded the latest gate verdict in every iteration trace
- required non-empty auditable evidence when completing or skipping an action
- exposed the action resolution evidence rule in capabilities and gateway schemas

## 1.19.0 - 2026-10-08

- rejected unknown baseline IDs instead of silently comparing against the newest baseline
- persisted regression gate summaries on provider runs and surfaced the latest gate in reports
- exposed latest gate decisions through bounded agent context and capabilities metadata

## 1.18.0 - 2026-10-07

- made regression gates baseline-aware without changing no-baseline compatibility
- scoped comparison evidence to the current provider run via observation run IDs
- added explicit `flat`, `mixed`, and `insufficient_data` gate outcomes and typed actions
- exposed `--baseline-id` through the CLI and `geiter_regression`

## 1.17.0 - 2026-10-07

- added the zero-dependency opt-in `http_json` live provider adapter
- exposed endpoint, headers, and timeout configuration through CLI and gateway
- classified retryable HTTP statuses without weakening deterministic fixture runs

## 1.16.0 - 2026-10-07

- added a bounded read-only agent bootstrap context
- exposed `context` through the CLI, `geiter_context`, and `geiter://context`
- centralized status payload generation across CLI and gateway

## 1.15.0 - 2026-10-07

- exposed agent-authored action proposals through the CLI and gateway
- documented action queue operations and expanded gateway discovery guidance

## 1.14.0 - 2026-10-07

- added one-hour leases to claimed agent actions
- added stale action discovery and idempotent reclaim through CLI and gateway
- made `iterate` recover expired claims before selecting its next action

## 1.13.0 - 2026-10-07

- made `iterate` claim the highest-priority open agent action
- added explicit completed/skipped action resolution with evidence
- exposed action skip and capability discovery through the gateway contract

## 1.12.1 - 2026-10-08

- surfaced open action counts and prioritized action records in status and reports
- documented the action queue contract in capabilities metadata

## 1.12.0 - 2026-10-08

- added a persistent, priority-ordered agent action queue
- persisted regression gate actions with run evidence and deduplication
- exposed action listing and evidence-backed completion through CLI and gateway

## 1.11.1 - 2026-10-08

- added CI smoke verification for the built wheel and installed CLI

## 1.11.0 - 2026-10-08

- added the stable `geiter://capabilities` discovery resource
- exposed the same capability document through `geiter_capabilities`

## 1.10.0 - 2026-10-08

- advertised tools and resources capabilities during gateway initialization
- added standard JSON-RPC invalid-request and internal-error responses

## 1.9.0 - 2026-10-08

- added typed next-step actions to regression gate results
- made failed checks directly actionable for agent callers

## 1.8.1 - 2026-10-07

- rejected empty regression batches instead of treating them as passing gates

## 1.8.0 - 2026-10-07

- added deterministic provider regression gate with machine-readable checks
- exposed regression runs through the CLI and agent gateway

## 1.7.0 - 2026-10-07

- added client-ready agent connection configuration output
- added `connect` CLI formats for generic, Claude, Cursor, and VS Code clients
- exposed `geiter_connect` through the JSON-RPC gateway

## 1.6.0 - 2026-10-07

- added durable run resumption by run ID
- exposed resumable runs through the CLI and agent gateway
- made run status distinguish active retry work from terminal partial runs

## 1.5.0 - 2026-10-07

- added attempt numbers and duration metadata
- classified provider errors as retryable or non-retryable
- stopped retrying configuration/value failures
- included failure classes in provider health summaries

## 1.4.0 - 2026-10-07

- added bounded retries for failed provider prompts
- ensured successful prompts are never replayed
- added exhausted-prompt counts to run summaries

## 1.3.0 - 2026-10-07

- added durable provider run ledgers
- recorded per-prompt failures without aborting the batch
- exposed recent run summaries through reports and `geiter_runs`

## 1.2.0 - 2026-10-07

- added observation quality metadata and provider health report
- filtered empty, invalid-citation, and duplicate observations from aggregates
- exposed quality health through CLI, agent gateway, and report output

## 1.1.0 - 2026-10-07

- added citation reciprocal-rank metrics with consistent improvement direction
- made baseline comparisons pair prompt/provider observations
- added an `insufficient_data` verdict for underpowered comparisons
- strengthened evaluation regression coverage

## 1.0.0 - 2026-10-07

- added provider and prompt-intent coverage matrix analysis
- added weakest-cell recommendations for agent prioritization
- promoted the stable agent-native GEO contract to version 1.0

## 0.9.0 - 2026-10-07

- made `iterate` evidence-aware
- linked comparison verdicts and pending experiments into actions and learnings
- exposed self-iteration through the agent gateway

## 0.8.0 - 2026-10-07

- added experiment result ledger with evidence and comparison linkage
- added explicit completion gate: only approved experiments can record results
- included experiment summaries and recent learnings in reports

## 0.7.0 - 2026-10-07

- added baseline snapshots and post-baseline delta comparisons
- added explicit improved/regressed/mixed/insufficient-data verdicts
- added policy-gated experiment proposals and approvals
- fixed comparison semantics so baseline samples are not re-counted

## 0.6.0 - 2026-10-07

- rebuilt README as an ASCII, agent-first project entry point
- added project contract tests for encoding, version drift, and release triggers
- clarified current capabilities and the next engineering frontier

## 0.5.0 - 2026-10-07

- added provider entry-point discovery for third-party adapters
- added MCP-style `resources/list` and `resources/read`
- documented plugin contracts and resource URIs

## 0.4.0 - 2026-10-07

- added provider protocol and deterministic JSONL replay adapter
- added `run` for batch observations across all prompts
- added provider replay contract tests
- added write-free syntax verification for cross-platform CI

## 0.3.0 - 2026-10-07

- added a stdio JSON-RPC gateway for agent and MCP-style callers
- added `doctor` consistency checks
- added complete machine-readable `report` snapshots
- added protocol-level gateway tests

## 0.2.0 - 2026-10-07

- added deterministic retrieval prompts
- added provider observation records with answer hashes and citation metrics
- added analysis output with mention rate, citation rate, and next action
- added Python 3.10-3.13 GitHub Actions coverage
- documented agent-first contribution rules

## 0.1.0 - 2026-10-07

- introduced the dependency-free self-iteration runtime
- added durable JSON state and append-only events
- added goals, memories, observations, hypotheses, actions, measurements, and learnings
