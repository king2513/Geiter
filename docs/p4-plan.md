# P4 Preparation Plan: Geiter Ecosystem Infrastructure

Status: preparation only. Formal P4 implementation starts only after explicit
user authorization.

## 1. Strategic correction

P4 is not another round of GEO feature expansion. P4 is the ecosystem phase:
turn Geiter from a capable agent-native project into an extensible
infrastructure platform.

The core should become a stable protocol and runtime. Providers, evaluators,
surfaces, planners, storage backends, agent tools, and integrations should be
able to evolve outside the kernel and compose through versioned contracts.

The north-star question for P4 is:

> Can an external developer build a useful Geiter extension, discover it,
> install it, run it safely, and expose it to agents without modifying the
> Geiter core?

## 2. Current baseline

P0-P3 already provide the foundation:

- durable JSON state and append-only events;
- deterministic prompts, observations, GEO metrics, health, score, and matrix;
- provider run ledgers, retries, resumable runs, and a first provider
  entry-point mechanism;
- baselines, comparisons, regression gates, durable gate actions, and
  owner-aware action claims;
- evidence-aware iteration, bounded agent bootstrap context, and stable
  capabilities discovery;
- sandbox execution, rollback, surface policies, approval workflow, and
  compounding experience;
- stdio JSON-RPC/MCP-style tools and resources;
- modular store composition while preserving `GeiterStore` as the public entry
  point.

The current release baseline is `1.26.2`. The existing plugin capability is
intentionally narrow: `geiter.providers` can discover a provider factory
implementing `answer(prompt) -> ProviderAnswer`. That is a useful seed, but it
does not yet define a general extension system.

## 3. P4 objective

P4 establishes the Geiter Extension Platform:

1. define a general plugin manifest and capability model;
2. support multiple extension kinds, not only providers;
3. make extensions discoverable to agents through capabilities and resources;
4. provide lifecycle, compatibility, configuration, and failure contracts;
5. isolate extension effects and make permissions explicit;
6. give external developers a reliable SDK, fixtures, examples, and test kit;
7. create the conditions for a real Geiter ecosystem without destabilizing the
   kernel.

## 4. Extension model

The plugin system should distinguish extension kinds by responsibility:

- `provider`: retrieves or replays external answers;
- `evaluator`: derives typed evidence and quality signals;
- `surface`: reads, snapshots, applies, and restores knowledge surfaces;
- `planner`: proposes bounded next actions or experiments;
- `store`: supplies an alternate persistence backend;
- `transport`: exposes Geiter through an agent protocol or host runtime;
- `observer`: emits external events or telemetry into the audit model;
- `command`: contributes agent-callable operations with declared side effects.

An extension must not reach into private `GeiterStore` internals. It receives a
versioned host context and communicates through declared contracts.

## 5. P4 workstreams

### P4.1 Extension protocol and manifest

Priority: highest.

Define a manifest that identifies:

- stable plugin ID and display name;
- extension kind and supported API versions;
- package/version and runtime requirements;
- exported capabilities, tools, resources, and schemas;
- configuration keys and secret references;
- read/write/network/process permissions;
- deterministic, replayable, or live behavior;
- health checks and failure classifications.

Candidate contracts:

```text
geiter/plugin-v1
geiter/plugin-manifest-v1
geiter/extension-capabilities-v1
```

The manifest becomes the unit of discovery. Python entry points may remain a
transport for installation, but they must no longer be the whole contract.

### P4.2 Extension registry and discovery

Priority: highest.

Build a registry that can discover installed extensions, validate manifests,
resolve IDs and versions, and expose a machine-readable catalog to agents.

Required surfaces:

- `geiter plugins list`
- `geiter plugins inspect <plugin-id>`
- `geiter_plugins` gateway tool
- `geiter://plugins` resource
- plugin metadata inside `geiter_capabilities`

Discovery must be read-only and must not import or execute extension actions
just to list them. Invalid plugins are reported with typed diagnostics instead
of breaking the whole runtime.

Candidate contract:

```text
geiter/plugin-catalog-v1
```

### P4.3 Extension lifecycle and host SDK

Priority: highest.

Define the host-side lifecycle:

```text
discover -> validate -> configure -> activate -> health-check -> invoke
        -> observe result -> deactivate
```

The SDK should provide:

- typed host context;
- namespaced configuration;
- structured logging and event emission;
- state/event access through approved interfaces;
- cancellation, timeout, retry, and resume hooks;
- dry-run and replay modes;
- stable error and health objects.

Lifecycle rules:

- discovery does not imply activation;
- activation does not imply permission to mutate;
- every invocation has a correlation ID;
- every external effect emits a receipt;
- extension failure is isolated and recorded;
- unsupported API versions fail before side effects.

### P4.4 First-party extension families

Priority: high.

Use the new protocol to migrate the current provider mechanism and add
reference implementations:

- provider extension: current `fixture`, `jsonl`, and `http_json` behavior;
- evaluator extension: a replayable metric/evidence evaluator;
- surface extension: the existing sandbox surface;
- transport extension: the existing stdio gateway;
- store extension: a no-op or read-only reference backend for contract tests.

The purpose is not feature volume. These reference plugins prove that the
extension boundary can represent the capabilities Geiter already owns.

The kernel must preserve compatibility with the current provider API during a
deprecation window.

### P4.5 Permissions, isolation, and trust

Priority: high.

Plugins are executable code and must be treated as untrusted integrations.
Define:

- permission classes: state-read, state-write, network, filesystem, process,
  external-publish;
- default-deny behavior for undeclared permissions;
- policy mapping from plugin capability to surface policy;
- trusted, reviewed, and local-development plugin states;
- timeout and resource limits where the host can enforce them;
- secret references that never enter state or event payloads;
- audit records for activation, invocation, denial, failure, and effect receipt.

The first implementation may use process-level boundaries and explicit policy
checks. Strong sandboxing can remain a later platform concern, but the
contract must not assume that all plugins are trusted.

### P4.6 Developer kit and ecosystem loop

Priority: high.

Ship the tools that make extension authorship practical:

- plugin template command;
- minimal provider/evaluator/surface examples;
- local manifest validator;
- replay fixture generator;
- contract-test helpers;
- compatibility checker;
- documentation for packaging and publishing;
- a versioned extension compatibility matrix.

Candidate commands:

```text
geiter plugin init
geiter plugin validate
geiter plugin test
geiter plugin inspect
```

The first successful plugin path should be short: scaffold, declare, run
deterministic fixture tests, install locally, discover through the agent
gateway, and invoke with an auditable result.

### P4.7 Optional ecosystem registry

Priority: later, after local discovery is stable.

Prepare a registry format and trust model for sharing plugins. The first
release should not require a hosted marketplace. A signed or reviewed index,
package metadata, compatibility information, and source links are sufficient.

The registry must not become a hidden runtime dependency. Offline discovery and
local installation remain supported.

Candidate contract:

```text
geiter/registry-index-v1
```

## 6. Agent-facing contract requirements

Every extension capability must be discoverable before invocation:

- plugin ID, kind, version, and API compatibility;
- exported tools and resources;
- required configuration and secret references;
- declared permissions and side effects;
- deterministic/replay/live mode;
- health state and last diagnostic;
- invocation schema and result schema;
- idempotency, retryability, and resumability;
- correlation ID and evidence references.

The bounded `context` packet should include only catalog summaries and urgent
diagnostics. Full plugin manifests, schemas, and logs belong behind explicit
resources or paginated tools.

## 7. Compatibility and migration

1. Keep `GeiterStore` and all existing `geiter/*-v1` contracts stable.
2. Treat the current `geiter.providers` entry-point group as a legacy
   compatibility adapter during P4.
3. Add new extension APIs beside the existing provider protocol.
4. Validate plugin API versions before loading executable code.
5. Preserve JSON state and append-only events as the audit spine.
6. Backfill plugin metadata without rewriting historical observations.
7. Make plugin activation explicit in capabilities and reports.
8. Deprecate, but do not remove, the old provider factory contract until a
   documented migration path and compatibility tests exist.

## 8. Test and release matrix

Every P4 extension release must cover:

- manifest parsing, schema validation, and version negotiation;
- discovery without activation;
- invalid, missing, and conflicting plugins;
- permission denial before side effects;
- timeout, retry, cancellation, resume, and idempotency;
- plugin failure isolation and audit records;
- deterministic replay fixtures;
- old provider plugin compatibility;
- CLI, gateway tool, and resource contracts;
- concurrent activation and action ownership;
- wheel build, installed CLI smoke test, syntax check, and `git diff --check`.

Reference plugins should be tested in-process. Isolation and permission tests
should also cover a subprocess boundary where supported.

## 9. Proposed release order

1. `P4.1`: manifest, extension kinds, API versioning, and catalog schemas.
2. `P4.2`: registry/discovery surfaces and plugin diagnostics.
3. `P4.3`: host SDK, lifecycle, permissions, and invocation receipts.
4. `P4.4`: migrate providers and ship reference evaluator/surface/transport
   extensions.
5. `P4.5`: developer kit, template, validator, fixture tools, and compatibility
   suite.
6. `P4.6`: optional external registry/index format.

Each increment must include capability metadata, contract tests, migration notes,
and a working example that an agent can discover and invoke.

## 10. Authorization boundary

This document is preparation only. No P4 implementation is authorized until
the user explicitly gives the start signal.