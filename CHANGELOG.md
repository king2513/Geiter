# Changelog

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
