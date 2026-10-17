#!/usr/bin/env python3
"""test-blindspot — map public symbols to the tests that never mention them.

Zero dependencies, Python 3.9+. Two evidence sources:

  * static reference matching — every public function/method/class in the
    source tree is looked up in the test corpus by identifier;
  * optional coverage.json (pytest-cov / coverage.py JSON) — symbols whose
    entire line range was never executed are called out separately.

Rules:
    BS001  public function or method has no reference in the test corpus
    BS002  public class has no reference in the test corpus
    BS003  coverage data shows 0% of a symbol's lines executed
    BS004  test file contains no test functions

Static matching is name-based, not resolved. It under-reports gaps rather
than inventing them. See SKILL.md for the boundaries.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "1.0.0"

RULES: Dict[str, Tuple[str, str]] = {
    "BS001": ("medium", "public function/method has no test reference"),
    "BS002": ("medium", "public class has no test reference"),
    "BS003": ("high", "symbol has zero executed lines in coverage data"),
    "BS004": ("low", "test file contains no test functions"),
}

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

TEST_FILE_RE = re.compile(r"(^test_.*\.py$|.*_test\.py$)")
TEST_FUNC_RE = re.compile(r"^test(_|$)")
SUPPRESS_RE = re.compile(r"#\s*blindspot:\s*ignore(?:=([A-Za-z0-9,\s]+))?")
FILE_IGNORE_RE = re.compile(r"#\s*blindspot:\s*ignore-file\b")

DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", "site-packages",
}


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # function | method | class
    path: str
    line: int
    end_line: int
    body_line: int = 0
    exported: bool = False
    decorator_lines: List[int] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.path, self.qualname, self.kind)


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    symbol: str
    kind: str
    message: str
    snippet: str = ""
    extra: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        data = {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "kind": self.kind,
            "message": self.message,
            "snippet": self.snippet,
        }
        data.update(self.extra)
        return data


@dataclass
class SourceFile:
    path: Path
    display: str
    lines: List[str]
    symbols: List[Symbol] = field(default_factory=list)
    parsed: bool = True
    ignored: bool = False
    suppressions: Dict[int, Set[str]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def iter_python_files(paths: Sequence[str], excludes: Set[str]) -> Iterable[Path]:
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix == ".py":
                yield p
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in excludes and not d.startswith("."))
            for name in sorted(files):
                if name.endswith(".py"):
                    yield Path(root) / name


def parse_suppressions(lines: List[str]) -> Tuple[Dict[int, Set[str]], bool]:
    per_line: Dict[int, Set[str]] = {}
    ignored = False
    for idx, text in enumerate(lines, 1):
        if text.lstrip().startswith("#") and FILE_IGNORE_RE.search(text):
            ignored = True
        m = SUPPRESS_RE.search(text)
        if not m:
            continue
        raw = m.group(1)
        if not raw or not raw.strip():
            per_line.setdefault(idx, set()).add("*")
        else:
            per_line.setdefault(idx, set()).update(
                part.strip().upper() for part in raw.split(",") if part.strip())
    return per_line, ignored


def suppressed(per_line: Dict[int, Set[str]], lines: Iterable[int], rule: str) -> bool:
    for line in lines:
        rules = per_line.get(line)
        if rules and ("*" in rules or rule in rules):
            return True
    return False


# --------------------------------------------------------------------------
# symbol extraction
# --------------------------------------------------------------------------

def decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def module_all(tree: ast.Module) -> Set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return set()
                    if isinstance(value, (list, tuple, set)):
                        return {item for item in value if isinstance(item, str)}
    return set()


def body_start(node: ast.AST) -> int:
    body = getattr(node, "body", None) or []
    if not body:
        return getattr(node, "lineno", 0)
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
            and isinstance(first.value.value, str):
        if len(body) > 1:
            return body[1].lineno
    return first.lineno


def decorator_lines(node: ast.AST) -> List[int]:
    return [getattr(d, "lineno", getattr(node, "lineno", 0)) for d in getattr(node, "decorator_list", [])]


def extract_symbols(tree: ast.Module, display: str, include_private: bool) -> List[Symbol]:
    exported = module_all(tree)
    symbols: List[Symbol] = []
    seen: Set[Tuple[str, str]] = set()

    def is_public(name: str) -> bool:
        return include_private or not name.startswith("_")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not is_public(node.name):
                continue
            if any(decorator_name(d).endswith("overload") for d in node.decorator_list):
                continue
            key = (node.name, "function")
            if key in seen:
                continue
            seen.add(key)
            symbols.append(Symbol(
                name=node.name, qualname=node.name, kind="function",
                path=display, line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                body_line=body_start(node), exported=node.name in exported,
                decorator_lines=decorator_lines(node),
            ))
        elif isinstance(node, ast.ClassDef):
            if not is_public(node.name):
                continue
            ckey = (node.name, "class")
            if ckey not in seen:
                seen.add(ckey)
                symbols.append(Symbol(
                    name=node.name, qualname=node.name, kind="class",
                    path=display, line=node.lineno, end_line=getattr(node, "end_lineno", node.lineno),
                    body_line=body_start(node), exported=node.name in exported,
                    decorator_lines=decorator_lines(node),
                ))
            for sub in node.body:
                if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not is_public(sub.name) or sub.name.startswith("__"):
                    continue
                if any(decorator_name(d).endswith("overload") for d in sub.decorator_list):
                    continue
                qualname = f"{node.name}.{sub.name}"
                key = (qualname, "method")
                if key in seen:
                    continue
                seen.add(key)
                symbols.append(Symbol(
                    name=sub.name, qualname=qualname, kind="method",
                    path=display, line=sub.lineno, end_line=getattr(sub, "end_lineno", sub.lineno),
                    body_line=body_start(sub), exported=sub.name in exported,
                    decorator_lines=decorator_lines(sub),
                ))
    return symbols


# --------------------------------------------------------------------------
# test corpus
# --------------------------------------------------------------------------

@dataclass
class TestCorpus:
    identifiers: Set[str] = field(default_factory=set)
    test_functions: int = 0
    files: int = 0
    findings: List[Finding] = field(default_factory=list)


def count_test_functions(tree: ast.Module) -> int:
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and TEST_FUNC_RE.match(node.name):
            total += 1
    return total


def call_attr(node: ast.AST) -> str:
    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def collect_corpus(paths: Sequence[str], excludes: Set[str]) -> TestCorpus:
    corpus = TestCorpus()
    for path in iter_python_files(paths, excludes):
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            print(f"test-blindspot: cannot read {path}: {exc}", file=sys.stderr)
            continue
        corpus.files += 1
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError) as exc:
            msg = getattr(exc, "msg", str(exc))
            print(f"test-blindspot: skipping {path}: {msg}", file=sys.stderr)
            continue
        count = count_test_functions(tree)
        corpus.test_functions += count
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                corpus.identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                corpus.identifiers.add(node.attr)
            elif isinstance(node, ast.alias):
                # from pkg import parse as p — both names are references.
                if node.asname:
                    corpus.identifiers.add(node.asname)
                corpus.identifiers.add(node.name.split(".")[-1])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.strip()
                if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", text):
                    corpus.identifiers.add(text.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Call) and call_attr(node) in {
                    "getattr", "setattr", "hasattr", "delattr", "patch"}:
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and re.fullmatch(r"[A-Za-z_]\w*", arg.value):
                        corpus.identifiers.add(arg.value)
        if TEST_FILE_RE.match(path.name) and count == 0:
            try:
                display = os.path.relpath(path, Path.cwd())
                if display.startswith(".."):
                    display = str(path)
            except ValueError:
                display = str(path)
            corpus.findings.append(Finding(
                rule="BS004", severity=RULES["BS004"][0], path=display,
                line=1, symbol=path.name, kind="file",
                message="test file contains no test functions",
                snippet=source.splitlines()[0].strip() if source.splitlines() else "",
            ))
    return corpus


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

@dataclass
class CoverageFile:
    executed: Set[int]
    missing: Set[int]


def normalize_path(text: str) -> str:
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def load_coverage(path: str) -> Dict[str, CoverageFile]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError("expected a coverage.py JSON report with a 'files' object")
    out: Dict[str, CoverageFile] = {}
    for name, entry in data["files"].items():
        if not isinstance(entry, dict):
            continue
        executed = {int(x) for x in (entry.get("executed_lines") or []) if isinstance(x, int)}
        missing = {int(x) for x in (entry.get("missing_lines") or []) if isinstance(x, int)}
        out[normalize_path(name)] = CoverageFile(executed=executed, missing=missing)
    return out


def resolve_coverage(displays: Sequence[str], cov: Dict[str, CoverageFile]) -> Dict[str, CoverageFile]:
    """Assign each coverage entry to the most specific source file.

    When a bare basename matches two different source files, the entry is
    ambiguous and is ignored rather than applied to both.
    """
    mapping: Dict[str, CoverageFile] = {}
    unique_displays = sorted(set(displays))
    for key, value in cov.items():
        candidates = [d for d in unique_displays
                      if d == key or d.endswith(key) or key.endswith(d)]
        if not candidates:
            continue
        longest = max(len(d) for d in candidates)
        best = [d for d in candidates if len(d) == longest]
        if len(set(best)) != 1:
            continue
        mapping[best[0]] = value
    return mapping


def coverage_stats(symbol: Symbol, cov: CoverageFile) -> Tuple[int, int, Optional[float]]:
    if symbol.kind == "class" or not symbol.body_line:
        return 0, 0, None
    span = range(symbol.body_line, symbol.end_line + 1)
    executed = sum(1 for line in span if line in cov.executed)
    missed = sum(1 for line in span if line in cov.missing)
    total = executed + missed
    pct = (executed / total * 100.0) if total else None
    return executed, missed, pct


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyze_sources(paths: Sequence[str], excludes: Set[str], include_private: bool,
                    test_roots: Sequence[str] = ()) -> List[SourceFile]:
    files: List[SourceFile] = []
    roots = [Path(p).resolve() for p in test_roots]
    for path in iter_python_files(paths, excludes):
        resolved = path.resolve()
        if any(_is_under(resolved, root) for root in roots):
            continue
        try:
            display = os.path.relpath(path, Path.cwd())
            if display.startswith(".."):
                display = str(path)
        except ValueError:
            display = str(path)
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            print(f"test-blindspot: cannot read {display}: {exc}", file=sys.stderr)
            record.parsed = False
            files.append(record)
            continue
        lines = source.splitlines()
        suppressions, ignored = parse_suppressions(lines)
        record = SourceFile(path=path, display=display, lines=lines, ignored=ignored,
                            suppressions=suppressions)
        if ignored:
            files.append(record)
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError) as exc:
            msg = getattr(exc, "msg", str(exc))
            print(f"test-blindspot: skipping {display}: {msg}", file=sys.stderr)
            record.parsed = False
            files.append(record)
            continue
        record.symbols = extract_symbols(tree, display, include_private)
        files.append(record)
    return files


def _is_under(path: Path, root: Path) -> bool:
    if root.is_file():
        return path == root
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def find_gaps(files: List[SourceFile], corpus: TestCorpus,
              coverage: Optional[Dict[str, CoverageFile]]) -> List[Finding]:
    findings: List[Finding] = []
    for record in files:
        if not record.parsed or record.ignored:
            continue
        suppressions = getattr(record, "suppressions", {})
        for symbol in record.symbols:
            lines = record.lines
            snippet = lines[symbol.line - 1].strip() if 0 < symbol.line <= len(lines) else ""

            def add(rule: str, message: str, extra: Optional[Dict[str, object]] = None) -> None:
                if suppressed(suppressions, [symbol.line] + symbol.decorator_lines, rule):
                    return
                findings.append(Finding(
                    rule=rule, severity=RULES[rule][0], path=record.display,
                    line=symbol.line, symbol=symbol.qualname, kind=symbol.kind,
                    message=message, snippet=snippet, extra=extra or {},
                ))

            if coverage is not None:
                cov = coverage.get(record.display)
                if cov is not None:
                    executed, missed, pct = coverage_stats(symbol, cov)
                    if symbol.kind != "class" and executed == 0 and missed > 0:
                        add("BS003",
                            f"{missed} executable line(s) in this symbol were never executed",
                            {"coverage_pct": 0.0, "missing_lines": missed})

            referenced = symbol.name in corpus.identifiers
            if referenced:
                continue
            if symbol.kind == "class":
                add("BS002", "public class is never referenced by the test corpus",
                    {"exported": symbol.exported})
            else:
                add("BS001", "public symbol is never referenced by the test corpus",
                    {"exported": symbol.exported})
    return findings


def dedupe(findings: List[Finding]) -> List[Finding]:
    seen: Set[Tuple[str, str, int, str]] = set()
    out: List[Finding] = []
    for f in findings:
        key = (f.rule, f.path, f.line, f.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def sort_findings(findings: List[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f.severity], f.path, f.line, f.rule))


def summarize(files: List[SourceFile], corpus: TestCorpus, findings: List[Finding],
              coverage_used: bool) -> Dict[str, object]:
    symbols = [s for f in files if f.parsed and not f.ignored for s in f.symbols]
    unreferenced = sum(1 for s in symbols if s.name not in corpus.identifiers)
    by_severity = {level: 0 for level in ("high", "medium", "low")}
    by_rule: Dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] += 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    total = len(symbols)
    return {
        "source_files": sum(1 for f in files if f.parsed and not f.ignored),
        "source_files_skipped": sum(1 for f in files if not f.parsed),
        "public_symbols": total,
        "symbols_without_test_reference": unreferenced,
        "reference_rate_pct": round((total - unreferenced) / total * 100.0, 1) if total else None,
        "test_files_scanned": corpus.files,
        "test_functions": corpus.test_functions,
        "coverage_used": coverage_used,
        "findings": len(findings),
        "by_severity": by_severity,
        "by_rule": dict(sorted(by_rule.items())),
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def render_text(summary: Dict[str, object], findings: List[Finding]) -> str:
    out: List[str] = []
    rate = summary["reference_rate_pct"]
    rate_text = f"{rate}% referenced" if rate is not None else "n/a"
    out.append(
        f"test-blindspot {VERSION}: {summary['public_symbols']} public symbols in "
        f"{summary['source_files']} files; {summary['symbols_without_test_reference']} "
        f"have no test reference ({rate_text})."
    )
    out.append(f"Test corpus: {summary['test_functions']} test functions in "
               f"{summary['test_files_scanned']} files.")
    if findings:
        out.append("")
        for f in sort_findings(findings):
            out.append(f"{f.severity.upper():<6} {f.rule}  {f.path}:{f.line}  {f.symbol}")
            out.append(f"       {f.message}")
    counts = summary["by_severity"]
    out.append("")
    out.append(f"{summary['findings']} findings "
               f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low)")
    return "\n".join(out)


def render_markdown(summary: Dict[str, object], findings: List[Finding]) -> str:
    lines = ["# test-blindspot report", ""]
    rate = summary["reference_rate_pct"]
    lines.append(
        f"Scanned **{summary['public_symbols']}** public symbols in "
        f"**{summary['source_files']}** source files. "
        f"**{summary['symbols_without_test_reference']}** have no reference in the test corpus"
        + (f" (reference rate **{rate}%**)." if rate is not None else ".")
    )
    lines.append("")
    lines.append(f"Test corpus: **{summary['test_functions']}** test functions in "
                 f"**{summary['test_files_scanned']}** files.")
    lines.append("")
    counts = summary["by_severity"]
    lines.append(f"Findings: **{summary['findings']}** "
                 f"({counts['high']} high / {counts['medium']} medium / {counts['low']} low)")
    lines.append("")
    if findings:
        lines.append("| Severity | Rule | Symbol | Location | Detail |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in sort_findings(findings):
            message = f.message.replace("|", "\\|")
            lines.append(f"| {f.severity} | {f.rule} | `{f.symbol}` | "
                         f"`{f.path}:{f.line}` | {message} |")
    else:
        lines.append("No findings.")
    if summary["by_rule"]:
        lines.append("")
        lines.append("## By rule")
        lines.append("")
        lines.append("| Rule | Count | Meaning |")
        lines.append("| --- | --- | --- |")
        for rule, count in summary["by_rule"].items():
            lines.append(f"| {rule} | {count} | {RULES.get(rule, ('', ''))[1]} |")
    lines.append("")
    return "\n".join(lines)


def write_text(path: str, text: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test-blindspot",
        description="Find public symbols that the test suite never mentions.",
    )
    parser.add_argument("sources", nargs="*", default=["."],
                        help="source files or directories (default: .)")
    parser.add_argument("--tests", action="append", default=[], metavar="DIR",
                        help="test file or directory (repeatable; default: ./tests)")
    parser.add_argument("--coverage", metavar="FILE",
                        help="coverage.py JSON report to cross-check executed lines")
    parser.add_argument("--json", metavar="FILE", help="write a JSON report")
    parser.add_argument("--md", metavar="FILE", help="write a Markdown report")
    parser.add_argument("--ignore", default="", metavar="RULES",
                        help="comma-separated rules to skip, e.g. BS004")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME",
                        help="directory name to skip (repeatable)")
    parser.add_argument("--include-private", action="store_true",
                        help="also audit names that start with an underscore")
    parser.add_argument("--fail-on", choices=["high", "medium", "low"], default=None,
                        help="exit 1 when a finding at or above this severity exists")
    parser.add_argument("--list-rules", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", action="version", version=f"test-blindspot {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for rule, (severity, title) in RULES.items():
            print(f"{rule}  {severity:<6}  {title}")
        return 0

    sources = args.sources or ["."]
    tests = args.tests or ["tests"]
    missing = [p for p in sources + tests if not Path(p).exists()]
    if missing:
        print(f"test-blindspot: path not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    ignored = {r.strip().upper() for r in args.ignore.split(",") if r.strip()}
    unknown = ignored - set(RULES)
    if unknown:
        print(f"test-blindspot: unknown rule(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    excludes = set(DEFAULT_EXCLUDES)
    excludes.update(args.exclude)

    try:
        coverage = load_coverage(args.coverage) if args.coverage else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"test-blindspot: cannot read coverage report: {exc}", file=sys.stderr)
        return 2

    files = analyze_sources(sources, excludes, args.include_private, tests)
    corpus = collect_corpus(tests, excludes)
    coverage_map = None
    if coverage is not None:
        coverage_map = resolve_coverage([f.display for f in files if f.parsed], coverage)
    findings = find_gaps(files, corpus, coverage_map) + corpus.findings
    findings = dedupe([f for f in findings if f.rule not in ignored])
    summary = summarize(files, corpus, findings, coverage is not None)

    if not args.quiet:
        print(render_text(summary, findings))
    if args.md:
        try:
            write_text(args.md, render_markdown(summary, findings))
        except OSError as exc:
            print(f"test-blindspot: cannot write {args.md}: {exc}", file=sys.stderr)
            return 2
        print(f"test-blindspot: wrote {args.md}", file=sys.stderr)
    if args.json:
        payload = {
            "tool": "test-blindspot",
            "version": VERSION,
            "summary": summary,
            "findings": [f.as_dict() for f in sort_findings(findings)],
        }
        try:
            write_text(args.json, json.dumps(payload, indent=2, ensure_ascii=False))
        except OSError as exc:
            print(f"test-blindspot: cannot write {args.json}: {exc}", file=sys.stderr)
            return 2
        print(f"test-blindspot: wrote {args.json}", file=sys.stderr)

    if args.fail_on:
        threshold = SEVERITY_RANK[args.fail_on]
        if any(SEVERITY_RANK[f.severity] >= threshold for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
