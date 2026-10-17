# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-17

### Added

- `test_blindspot.py`: public-symbol extraction with the stdlib AST, test-corpus
  reference matching, and optional `coverage.py` JSON cross-check.
- Rules BS001–BS004: unreferenced function/method, unreferenced class, 0% body
  coverage, test file without test functions.
- `--coverage`, `--include-private`, inline and file-level suppression.
- Text, Markdown and JSON reports; `--fail-on` exit codes for CI.
- 47 unit tests, GitHub Actions matrix (Python 3.9–3.12), banner and demo SVG.

### Fixed

Issues found by adversarial verification before the first release:

- BS003 never fired on real coverage reports because the `def` line is executed
  at import time. It now measures the function body.
- `from pkg import parse as p` and `mock.patch("pkg.parse")` did not count as
  references. Aliases, dotted string constants, and `getattr`-style strings are
  now collected.
- A bare coverage basename matching two source files was applied to both; the
  entry is now treated as ambiguous and ignored.
- NUL bytes, dangling symlinks, `executed_lines: null` and report paths that
  were directories crashed instead of being skipped or exiting 2.
- `# blindspot: ignore` on a decorator line did not suppress the finding.
- `# blindspot: ignore-file` was ignored after line 5 and was triggered by the
  marker appearing inside a string literal.
