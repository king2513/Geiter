# Geiter

> The self-improving GEO runtime for agents.

Geiter is an agent-native workspace for Generative Engine Optimization. It helps an agent inspect a knowledge surface, form a hypothesis, leave an auditable action trail, measure what happened, and carry the learning into the next loop.

The first release is dependency-free:

- durable local state in `.geiter/state.json`
- append-only event log in `.geiter/events.jsonl`
- stable JSON output for agent callers
- deterministic retrieval prompts and provider observations
- first GEO signals: mention rate, citation rate, citation position
- stdio JSON-RPC gateway for agent and MCP-style callers
- `doctor` consistency checks and `report` snapshots
- replayable provider adapters and batch observation runs
- installable provider plugins through `geiter.providers` entry points
- MCP-style resources for status and reports
- a deterministic loop: `inspect -> hypothesize -> act -> measure -> learn`

## Quick start

Requires Python 3.10+.

```bash
python -m geiter init
python -m geiter goal add "Make Geiter the clearest agent-native GEO runtime"
python -m geiter memory add principle "Agents should inspect Geiter without reading prose."
python -m geiter prompt add "What is Geiter?" --intent discovery
python -m geiter prompt list --json
# Use the returned prompt id:
python -m geiter observe <prompt-id> --provider fixture --answer "Geiter is an agent-native GEO runtime." --citation https://example.com/geiter
python -m geiter analyze --json
python -m geiter doctor --json
python -m geiter report --json
python -m geiter run --provider jsonl --fixture ./answers.jsonl --json
python -m geiter inspect
python -m geiter iterate --hypothesis "Expose one canonical JSON contract"
python -m geiter status --json
```

Use `--root` to point Geiter at another workspace:

```bash
python -m geiter --root ./demo status --json
```

## Core model

```text
Workspace
  ├── identity       what Geiter is trying to become
  ├── goals          desired outcomes
  ├── memories       durable principles and facts
  ├── prompts        retrieval questions to probe
  ├── observations   what the agent currently sees
  ├── hypotheses     explanations worth testing
  ├── actions        changes or experiments
  ├── measurements   evidence of effect
  └── learnings      what should change in the next loop
```

The runtime does not pretend to be a full crawler, search engine, or LLM. Those capabilities can attach through adapters without changing the core contract.

## Why Geiter exists

Most GEO tooling optimizes pages for human readers and treats agents as an analytics channel. Geiter reverses that relationship:

1. Agent-readable first: important operations have stable JSON output.
2. Evidence over vibes: observations, hypotheses, actions, and measurements are typed records.
3. Self-iteration: every cycle ends by writing a learning.
4. Portable by default: the kernel uses only the Python standard library.
5. Composable: the CLI can later be wrapped by MCP, HTTP, or another orchestrator.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Create a Geiter workspace |
| `status` | Read the current state summary |
| `inspect` | Record a fresh workspace observation |
| `goal add/list` | Manage desired outcomes |
| `memory add/list` | Manage durable agent memory |
| `prompt add/list` | Manage deterministic retrieval prompts |
| `observe` | Record a provider answer and citations |
| `analyze` | Compute GEO signals and next action |
| `doctor` | Validate state and event consistency |
| `report` | Emit a complete machine-readable report |
| `gateway` | Serve the stdio agent gateway |
| `run` | Replay a provider fixture across all prompts |
| `iterate` | Run one self-iteration cycle |
| `event list` | Inspect the event stream |

## Development

```bash
python -m unittest discover -s tests -v
python -m geiter --help
```

Agent gateway smoke test:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python -m geiter gateway
```

Provider fixtures are JSONL so runs are deterministic and reviewable:

```json
{"prompt":"What is Geiter?","provider":"replay","answer":"Geiter is an agent-native GEO runtime.","citations":["https://geiter.dev/docs"]}
```

### Provider plugins

Third-party packages can register a provider without changing Geiter:

```toml
[project.entry-points."geiter.providers"]
my-provider = "my_package.provider:factory"
```

The factory receives provider-specific keyword arguments and returns an object
implementing `answer(prompt) -> {provider, answer, citations}`.

## Roadmap

- MCP tools and resources over the same domain contract
- repository and content adapters for real GEO observations
- retrieval probes and evaluator adapters
- policy gates for safe autonomous actions
- SQLite persistence when multi-run querying becomes necessary
