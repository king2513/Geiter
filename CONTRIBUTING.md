# Contributing to Geiter

Geiter is built for agents first. Contributions should preserve that property:

- keep the core dependency-free unless a dependency earns its place
- make state transitions typed, durable, and inspectable
- keep CLI output stable with `--json`
- prefer deterministic fixtures over live provider calls in tests
- never make autonomous publishing the default

## Local checks

```bash
python -m unittest discover -s tests -v
python tests/syntax_check.py
python -m geiter --help
```

For a new domain capability, add a small end-to-end fixture that proves an agent
can invoke it, inspect the result, and continue the loop.
