---
name: test-blindspot
description: Find public Python functions, methods and classes that the test suite never references, optionally cross-checked against coverage.py JSON. Use when asked what is untested, why coverage looks high while APIs are unexercised, or to gate a PR on test blindspots. Name-based static matching; it does not execute tests or build a call graph.
---

# test-blindspot

Use this skill when the question is **"what is not tested at all?"** at symbol
granularity, rather than "which lines ran?". It maps the public surface of a
source tree to identifiers used in a test corpus and, when given coverage data,
separates "never mentioned" from "mentioned but never executed".

## Quick start

```bash
python3 scripts/test_blindspot.py src --tests tests
python3 scripts/test_blindspot.py src --tests tests --coverage coverage.json
python3 scripts/test_blindspot.py . --tests tests --include-private
python3 scripts/test_blindspot.py src --tests tests --fail-on high
```

- `sources` defaults to `.`; files under `--tests` are excluded from the source
  scan.
- `--tests` is repeatable and defaults to `tests`.
- `--coverage` expects the JSON produced by `coverage json`.
- `--list-rules` prints the rule table.

## Rules

| Rule | Meaning | Default |
| --- | --- | --- |
| BS001 | public function/method never referenced by tests | medium |
| BS002 | public class never referenced by tests | medium |
| BS003 | 0% of the symbol body executed in coverage data | high |
| BS004 | file named like a test but with no test functions | low |

## How to triage

1. `BS003` is the strongest signal: real coverage says the body never ran.
2. `BS001` / `BS002` are reference gaps, not proof of missing behavior. Check
   first whether the symbol is exercised indirectly (HTTP client, CLI
   subprocess, `mock.patch`, decorator registration).
3. `BS004` usually means a fixture or a disabled test module.
4. Suppress intentional gaps on the definition line; do not delete the finding
   by ignoring the whole file.

## Suppression

```python
def legacy():  # blindspot: ignore=BS001
    ...
```

`# blindspot: ignore` covers all rules for the symbol, including when placed on
a decorator line. `# blindspot: ignore-file` (in a comment) skips the file.

## Reporting contract

- `--json` writes summary + findings; `--md` writes a table.
- `--fail-on high|medium|low` controls the exit code; the JSON summary always
  reports the raw reference rate.
- Exit 2 for missing paths, unknown rules, or a malformed coverage report.

## Guardrails

- Name-based matching only. There is no call graph, no type inference, and no
  framework awareness.
- A common method name in a test helper can mask an untested method. The tool
  documents this rather than guessing.
- `BS003` uses the function body, not the `def` line, because coverage marks the
  signature executed at import time.
- Ambiguous coverage keys (one basename, two source files) are ignored.
- Never edits files, executes tests, or calls the network.
- Python only.

## Requirements

Python 3.9+, standard library only.
