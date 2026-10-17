"""Tests for test_blindspot.py — stdlib unittest, no third-party dependencies."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_blindspot as tb  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.dir)
        (self.dir / "src").mkdir()
        (self.dir / "tests").mkdir()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_scan(self, extra=None, sources=("src",), tests=("tests",)):
        argv = list(sources) + ["--tests", tests[0]] + list(extra or [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tb.main(argv)
        return code, out.getvalue(), err.getvalue()

    def findings(self, extra=None):
        code, out, err = self.run_scan(extra)
        self.assertEqual(code, 0, err)
        return out

    def rules(self):
        code, _, _ = self.run_scan()
        # Re-run internal pipeline for structured checks.
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        corpus = tb.collect_corpus(["tests"], tb.DEFAULT_EXCLUDES)
        return [f.rule for f in tb.find_gaps(files, corpus, None)] + [f.rule for f in corpus.findings]


class TestReferences(Base):
    def test_unreferenced_function(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_nothing():\n    assert True\n")
        self.assertIn("BS001", self.rules())

    def test_referenced_function(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "from src.core import parse\n\ndef test_parse():\n    assert parse(1) == 1\n")
        self.assertNotIn("BS001", self.rules())

    def test_reference_through_attribute(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "import src.core as c\n\ndef test_parse():\n    assert c.parse(1) == 1\n")
        self.assertNotIn("BS001", self.rules())

    def test_private_function_skipped_by_default(self):
        self.write("src/core.py", "def _helper(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertNotIn("BS001", self.rules())

    def test_private_function_included_with_flag(self):
        self.write("src/core.py", "def _helper(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        code, out, _ = self.run_scan(["--include-private"])
        self.assertEqual(code, 0)
        self.assertIn("BS001", out)

    def test_class_unreferenced(self):
        self.write("src/model.py", "class Widget:\n    pass\n")
        self.write("tests/test_model.py", "def test_x():\n    assert True\n")
        self.assertIn("BS002", self.rules())

    def test_class_referenced(self):
        self.write("src/model.py", "class Widget:\n    pass\n")
        self.write("tests/test_model.py", "from src.model import Widget\n\ndef test_x():\n    assert Widget()\n")
        self.assertNotIn("BS002", self.rules())

    def test_test_class_name_does_not_count_as_reference(self):
        self.write("src/model.py", "class Widget:\n    pass\n")
        self.write("tests/test_model.py", "class TestWidget:\n    def test_x(self):\n        assert True\n")
        self.assertIn("BS002", self.rules())

    def test_methods_are_checked(self):
        self.write("src/model.py", "class Widget:\n    def render(self):\n        return 1\n")
        self.write("tests/test_model.py", "w = object()\ndef test_x():\n    assert True\n")
        self.assertIn("BS001", self.rules())

    def test_method_referenced_by_name(self):
        self.write("src/model.py", "class Widget:\n    def render(self):\n        return 1\n")
        self.write("tests/test_model.py", "def test_x(w):\n    assert w.render() == 1\n")
        self.assertNotIn("BS001", self.rules())

    def test_overload_duplicates_skipped(self):
        self.write("src/core.py", (
            "from typing import overload\n"
            "@overload\n"
            "def parse(x: int) -> int: ...\n"
            "@overload\n"
            "def parse(x: str) -> str: ...\n"
            "def parse(x):\n    return x\n"
        ))
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        funcs = [s for f in files for s in f.symbols if s.name == "parse"]
        self.assertEqual(len(funcs), 1)

    def test_property_setter_deduped(self):
        self.write("src/model.py", (
            "class Widget:\n"
            "    @property\n"
            "    def size(self):\n"
            "        return 1\n"
            "    @size.setter\n"
            "    def size(self, value):\n"
            "        pass\n"
        ))
        self.write("tests/test_model.py", "def test_x():\n    assert True\n")
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        methods = [s for f in files for s in f.symbols if s.name == "size"]
        self.assertEqual(len(methods), 1)

    def test_dunder_methods_skipped(self):
        self.write("src/model.py", "class Widget:\n    def __repr__(self):\n        return 'w'\n")
        self.write("tests/test_model.py", "def test_x():\n    assert True\n")
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        names = [s.name for f in files for s in f.symbols]
        self.assertNotIn("__repr__", names)

    def test_exported_flag(self):
        self.write("src/core.py", "__all__ = ['parse']\n\ndef parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        corpus = tb.collect_corpus(["tests"], tb.DEFAULT_EXCLUDES)
        findings = tb.find_gaps(files, corpus, None)
        self.assertTrue(any(f.extra.get("exported") is True for f in findings if f.rule == "BS001"))

    def test_async_function(self):
        self.write("src/core.py", "async def fetch(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertIn("BS001", self.rules())

    def test_nested_function_not_a_symbol(self):
        self.write("src/core.py", "def outer():\n    def inner():\n        return 1\n    return inner()\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        names = [s.name for f in files for s in f.symbols]
        self.assertEqual(names, ["outer"])


class TestCorpus(Base):
    def test_test_file_without_tests(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_empty.py", "HELPER = 1\n")
        self.assertIn("BS004", self.rules())

    def test_conftest_is_not_flagged(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/conftest.py", "import pytest\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertNotIn("BS004", self.rules())

    def test_helper_module_is_not_flagged(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/helpers.py", "def make():\n    return 1\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertNotIn("BS004", self.rules())

    def test_non_python_test_files_not_scanned(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.js", "test('x', () => {});\n")
        self.assertIn("BS001", self.rules())


class TestCoverage(Base):
    def write_cov(self, mapping):
        return self.write("coverage.json", json.dumps({"files": mapping}))

    def test_zero_executed_symbol(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        # Real coverage marks the def line executed at import time.
        self.write_cov({"src/core.py": {"executed_lines": [1], "missing_lines": [2]}})
        self.assertIn("BS003", self.rules_with_coverage())

    def rules_with_coverage(self):
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        corpus = tb.collect_corpus(["tests"], tb.DEFAULT_EXCLUDES)
        cov = tb.load_coverage("coverage.json")
        mapping = tb.resolve_coverage([f.display for f in files if f.parsed], cov)
        findings = tb.find_gaps(files, corpus, mapping) + corpus.findings
        return [f.rule for f in findings]

    def test_partially_executed_symbol_not_flagged(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.write_cov({"src/core.py": {"executed_lines": [2], "missing_lines": [1]}})
        self.assertNotIn("BS003", self.rules_with_coverage())

    def test_class_not_flagged_for_coverage(self):
        self.write("src/model.py", "class Widget:\n    pass\n")
        self.write("tests/test_model.py", "def test_x():\n    assert True\n")
        self.write_cov({"src/model.py": {"executed_lines": [], "missing_lines": [1, 2]}})
        self.assertNotIn("BS003", self.rules_with_coverage())

    def test_suffix_path_matching(self):
        self.write("src/pkg/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.write_cov({"core.py": {"executed_lines": [], "missing_lines": [1, 2]}})
        self.assertIn("BS003", self.rules_with_coverage())

    def test_malformed_coverage_exit_2(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.write("coverage.json", "{\"nope\": 1}")
        code, _, err = self.run_scan(["--coverage", "coverage.json"])
        self.assertEqual(code, 2)
        self.assertIn("coverage", err)

    def test_missing_coverage_file_exit_2(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        code, _, _ = self.run_scan(["--coverage", "nope.json"])
        self.assertEqual(code, 2)


class TestCliAndSuppression(Base):
    def setUp(self):
        super().setUp()
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")

    def test_inline_suppression(self):
        self.write("src/core.py", "def parse(x):  # blindspot: ignore=BS001\n    return x\n")
        self.assertNotIn("BS001", self.rules())

    def test_file_ignore(self):
        self.write("src/core.py", "# blindspot: ignore-file\ndef parse(x):\n    return x\n")
        self.assertNotIn("BS001", self.rules())

    def test_ignore_rule_cli(self):
        code, out, _ = self.run_scan(["--ignore", "BS001"])
        self.assertEqual(code, 0)
        self.assertNotIn("BS001", out)

    def test_fail_on_medium(self):
        code, _, _ = self.run_scan(["--fail-on", "high"])
        self.assertEqual(code, 0)  # BS001 is medium

    def test_fail_on_high_with_coverage(self):
        self.write("coverage.json", json.dumps({"files": {
            "src/core.py": {"executed_lines": [], "missing_lines": [1, 2]}}}))
        code, _, _ = self.run_scan(["--coverage", "coverage.json", "--fail-on", "high"])
        self.assertEqual(code, 1)

    def test_reports_written(self):
        code, _, _ = self.run_scan(["--json", "out.json", "--md", "out.md", "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "out.json").exists())
        self.assertTrue((self.dir / "out.md").exists())
        data = json.loads((self.dir / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["public_symbols"], 1)
        self.assertEqual(data["findings"][0]["rule"], "BS001")

    def test_source_scan_skips_test_directories(self):
        code, out, err = self.run_scan([], sources=(".",))
        self.assertEqual(code, 0, err)
        self.assertNotIn("test_x", out)

    def test_syntax_error_source_skipped(self):
        self.write("src/broken.py", "def broken(:\n    pass\n")
        code, _, err = self.run_scan()
        self.assertEqual(code, 0)
        self.assertIn("skipping", err)

    def test_empty_dirs(self):
        (self.dir / "src" / "core.py").unlink()
        (self.dir / "tests" / "test_core.py").unlink()
        code, out, _ = self.run_scan()
        self.assertEqual(code, 0)
        self.assertIn("0 public symbols", out)

    def test_missing_path_exit_2(self):
        code, _, _ = self.run_scan([], sources=("nope",))
        self.assertEqual(code, 2)

    def test_summary_rate(self):
        self.write("src/more.py", "def unused():\n    return 1\n")
        code, out, _ = self.run_scan(["--quiet", "--json", "out.json"])
        data = json.loads((self.dir / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["public_symbols"], 2)
        self.assertEqual(data["summary"]["symbols_without_test_reference"], 2)
        self.assertEqual(data["summary"]["reference_rate_pct"], 0.0)


class TestRegressionFixes(Base):
    """Cases found by adversarial verification."""

    def test_aliased_import_counts_as_reference(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py",
                   "from src.core import parse as p\n\ndef test_x():\n    assert p(1) == 1\n")
        self.assertNotIn("BS001", self.rules())

    def test_mock_patch_string_counts_as_reference(self):
        self.write("src/core.py", "def render(x):\n    return x\n")
        self.write("tests/test_core.py",
                   "def test_x():\n    with mock.patch('src.core.render'):\n        pass\n")
        self.assertNotIn("BS001", self.rules())

    def test_getattr_string_counts_as_reference(self):
        self.write("src/core.py", "def render(x):\n    return x\n")
        self.write("tests/test_core.py",
                   "def test_x(mod):\n    assert getattr(mod, 'render')\n")
        self.assertNotIn("BS001", self.rules())

    def test_coverage_null_lines_do_not_crash(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.write("coverage.json", json.dumps({"files": {
            "src/core.py": {"executed_lines": None, "missing_lines": None}}}))
        code, _, err = self.run_scan(["--coverage", "coverage.json"])
        self.assertEqual(code, 0, err)

    def test_ambiguous_basename_coverage_ignored(self):
        self.write("src/a/core.py", "def parse(x):\n    return x\n")
        self.write("src/b/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.write("coverage.json", json.dumps({"files": {
            "core.py": {"executed_lines": [1], "missing_lines": [2]}}}))
        files = tb.analyze_sources(["src"], tb.DEFAULT_EXCLUDES, False, ["tests"])
        cov = tb.load_coverage("coverage.json")
        mapping = tb.resolve_coverage([f.display for f in files if f.parsed], cov)
        self.assertEqual(mapping, {})

    def test_suppression_on_decorator_line(self):
        self.write("src/core.py", (
            "def _deco(fn):\n"
            "    return fn\n"
            "@_deco  # blindspot: ignore=BS001\n"
            "def parse(x):\n"
            "    return x\n"
        ))
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertNotIn("BS001", self.rules())

    def test_ignore_file_after_header(self):
        self.write("src/core.py", (
            '"""doc"""\n'
            "import os\n"
            "import sys\n"
            "\n"
            "CONST = 1\n"
            "# blindspot: ignore-file\n"
            "def parse(x):\n"
            "    return x\n"
        ))
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertNotIn("BS001", self.rules())

    def test_source_with_nul_bytes_is_skipped(self):
        (self.dir / "src" / "bad.py").write_bytes(b"def broken():\n    return 1\x00\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        code, _, err = self.run_scan()
        self.assertEqual(code, 0)
        self.assertIn("skipping", err)

    def test_marker_inside_string_does_not_disable_file(self):
        self.write("src/core.py", (
            "def parse(x):\n"
            '    marker = "# blindspot: ignore-file"\n'
            "    return x\n"
        ))
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        self.assertIn("BS001", self.rules())

    def test_report_path_is_directory_exit_2(self):
        self.write("src/core.py", "def parse(x):\n    return x\n")
        self.write("tests/test_core.py", "def test_x():\n    assert True\n")
        (self.dir / "out").mkdir()
        code, _, err = self.run_scan(["--json", "out", "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("cannot write", err)


if __name__ == "__main__":
    unittest.main()
