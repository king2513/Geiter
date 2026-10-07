# Changelog

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
