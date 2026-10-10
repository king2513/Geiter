# P4 Preparation Plan

Status: preparation only. P4 implementation starts only after explicit user
authorization.

## 1. Current baseline

Geiter has completed the P0-P3 foundation:

- durable JSON state and append-only events
- deterministic prompts, observations, GEO metrics, health, score, and matrix
- provider run ledgers, bounded retries, resumable runs, and provider plugins
- baselines, paired comparisons, regression gates, and durable gate actions
- agent action queue with deduplication, leases, stale recovery, and
  owner-aware claims
- evidence-aware iteration and bounded agent bootstrap context
- sandbox execution with snapshot/revert and a regression gate as arbiter
- explicit surface policies and durable approval workflow
- compounding experience that selects historically effective change kinds
- stdio JSON-RPC/MCP-style tools and resources with stable discovery metadata
- modular store composition while preserving `GeiterStore` as the public entry
  point

The current release baseline is `1.26.2`. The local JSON state remains the
source of truth, replay fixtures remain deterministic, and external mutation
requires an explicit policy decision.

## 2. P4 objective

P4 should make Geiter a stronger autonomous GEO research and execution
runtime, not just a larger collection of commands. The agent should be able
to:

1. collect comparable evidence from several retrieval surfaces;
2. evaluate answer quality beyond string mention and URL presence;
3. explain which evidence supports a decision and how confident that decision
   is;
4. schedule bounded experiments across providers and surfaces;
5. retain the current audit, replay, approval, and rollback guarantees while
   the state volume grows.

## 3. Non-goals

P4 does not turn Geiter into a general crawler, search engine, LLM, CMS, or
human-facing dashboard. It does not make live network access implicit, store
credentials in state, or bypass approval for real surfaces. It does not break
existing `geiter/*-v1` contracts merely to simplify implementation.

## 4. Proposed workstreams

### P4.1 Retrieval evaluation kernel

Priority: highest.

Introduce composable evaluators for:

- entity disambiguation and answer claim coverage;
- citation validity, source authority, freshness, and target attribution;
- answer consistency across repeated probes;
- provider-specific and cross-provider agreement;
- confidence and evidence sufficiency separate from the north-star score.

Preparation constraints:

- evaluators consume normalized observation records;
- every evaluator returns a typed result with evidence references;
- missing or unusable evidence remains explicit;
- existing metrics keep their meaning and remain available;
- replay fixtures cover every evaluator before live-provider validation.

Candidate contracts:

```text
geiter/evaluation-v1
geiter/evidence-v1
geiter/confidence-v1
```

### P4.2 Multi-provider evidence orchestration

Priority: high.

Add a provider plan that can run the same prompt set across named providers
with independent run ledgers, rate limits, retries, and partial completion.
The result must preserve provider identity and be comparable without treating
one provider as ground truth.

Preparation constraints:

- one provider failure cannot erase successful evidence from another;
- live and replay providers use the same normalized answer contract;
- credentials and endpoints remain runtime-only;
- an orchestration run is resumable and idempotent;
- the regression gate receives a typed provider matrix, not prose.

Candidate contracts:

```text
geiter/provider-plan-v1
geiter/orchestration-v1
```

### P4.3 Agent experiment planner

Priority: high.

Extend the existing action queue, baselines, experiments, and experience
surfaces into a bounded planner. The planner should select one next experiment
from evidence, estimate expected information gain, and record why it was
selected.

Preparation constraints:

- planner output is a proposal until policy authorizes execution;
- only one active change claim may own a surface/action at a time;
- experiment completion requires evidence and a gate result;
- failed or conflicting work becomes a durable action, never an implicit
  retry loop;
- planning remains deterministic when fed the same replay state.

Candidate contracts:

```text
geiter/plan-v1
geiter/experiment-run-v1
```

### P4.4 Durable query/storage boundary

Priority: medium, after the evaluation contracts stabilize.

Introduce a storage abstraction without replacing the JSON backend immediately.
SQLite should be an optional backend for indexed history and higher query
volume, while JSON remains the portable replay and migration format.

Preparation constraints:

- `GeiterStore` remains the public entry point;
- JSON and SQLite expose the same domain records and event semantics;
- migrations are versioned, one-way, inspectable, and reversible through export;
- credentials and provider configuration never enter either backend;
- the backend choice is explicit and reported in capabilities.

Candidate contracts:

```text
geiter/storage-v1
geiter/export-v1
```

### P4.5 Governed surface adapters

Priority: medium, after the planner and gate contracts stabilize.

Add adapters for real knowledge surfaces only behind the current policy model.
The first adapter should support read, snapshot, apply, and restore with
structured change receipts. Publishing remains an approved side effect, and
the regression gate remains the outcome arbiter.

Preparation constraints:

- no implicit writes;
- dry-run and replay mode for every adapter;
- pre-change snapshot and post-change receipt are mandatory;
- restore must be testable independently of the provider;
- adapter failures are typed and resumable.

## 5. Agent-facing contract requirements

Every P4 capability must be discoverable from `geiter_capabilities` and
`geiter://capabilities` before an agent uses it. Each new surface must define:

- schema name and version;
- CLI entrypoint and gateway tool;
- read versus write behavior;
- idempotency key and deduplication rule;
- retryability and resumability;
- evidence references and failure types;
- policy and approval boundary;
- replay fixture format;
- compatibility behavior for older workspaces.

The existing `context` packet must stay bounded. Large evidence belongs behind
explicit resources or paginated tools rather than being copied into bootstrap
context.

## 6. State and migration strategy

1. Keep current `geiter/v1` records readable.
2. Add new collections only through read-time backfill and explicit doctor
   checks.
3. Preserve append-only events as the audit spine.
4. Add stable references instead of duplicating full observations.
5. Export a complete JSON snapshot before any backend migration.
6. Require deterministic import/export round trips in tests.

## 7. Test matrix

Before each P4 release:

- unit tests for evaluator directionality and insufficient-data behavior;
- replay fixtures for every provider/evaluator combination;
- partial provider failure, retry, resume, and idempotency tests;
- concurrent agent claims and surface-write conflict tests;
- policy, approval, deny, rollback, and stale-work recovery tests;
- JSON contract tests for CLI, gateway tools, and resources;
- old-workspace backfill and migration round-trip tests;
- wheel build, installed CLI smoke test, syntax check, and `git diff --check`.

Live-provider tests remain opt-in and isolated from deterministic CI.

## 8. Release sequence

The proposed order is:

1. P4.1 evaluation contracts and replay fixtures.
2. P4.2 multi-provider orchestration.
3. P4.3 experiment planner integration.
4. P4.4 optional SQLite backend and export/import.
5. P4.5 governed surface adapters.

Each step should ship as a narrow versioned increment with a changelog entry,
capability metadata, contract tests, and a migration note when state changes.

## 9. Authorization boundary

This document records preparation only. No P4 workstream is authorized for
formal implementation until the user explicitly gives the start signal.
