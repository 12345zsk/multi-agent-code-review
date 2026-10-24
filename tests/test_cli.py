"""CLI and Git input integration in isolated temporary directories."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF
from src.cli import _read_sources, main


class CliTests(unittest.TestCase):
    def invoke(self, args):
        out, err = StringIO(), StringIO()
        working = Path(args[args.index("--file")+1]).resolve().parent if "--file" in args else Path.cwd()
        with chdir(working), patch("sys.argv", ["code-review", *args]), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                status = 0
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def test_graph_does_not_initialize_model(self):
        with patch("src.llm.get_fast_llm", side_effect=AssertionError("No model expected")):
            code, output, _ = self.invoke(["--graph"])
        self.assertEqual(code, 0)
        self.assertIn("regression_reviewer", output)

    def test_fix_exports_json_and_patches_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stats.py").write_text(CURRENT)
            (root / "input.diff").write_text(SAMPLE_DIFF)
            report = root / "custom" / "reports" / "report.json"
            patch_file = root / "custom" / "patches" / "repair.diff"
            test_file = root / "custom" / "tests" / "tests.diff"
            args = ["--file", str(root / "input.diff"), "--mode", "fix", "--source-root", str(root),
                    "--format", "json", "--output", str(report),
                    "--run-root", str(root / "runs"),
                    "--patch-output", str(patch_file), "--test-output", str(test_file)]
            with patch("src.llm.get_fast_llm", return_value=DemoModel()):
                code, _, error = self.invoke(args)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(report.read_text())["status"], "proposed")
            self.assertIn("+    if not values:", patch_file.read_text())
            self.assertIn("generated_tests/test_stats.py", test_file.read_text())
            self.assertEqual((root / "stats.py").read_text(), CURRENT)

    def test_review_defaults_to_output_under_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.diff").write_text(SAMPLE_DIFF)
            with chdir(root), patch("src.llm.get_fast_llm", return_value=DemoModel()):
                code, output, error = self.invoke(["--file", "input.diff"])
            report = root / "output" / "review.md"
            self.assertEqual(code, 0, error)
            self.assertIn("Status: completed", report.read_text())
            self.assertIn("Status: completed", output)
            self.assertIn(str(report), error)
            self.assertFalse((root / "output" / "repair.diff").exists())
            self.assertFalse((root / "output" / "tests.diff").exists())

    def test_fix_defaults_to_output_and_json_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.diff").write_text(SAMPLE_DIFF)
            (root / "stats.py").write_text(CURRENT)
            with chdir(root), patch("src.llm.get_fast_llm", return_value=DemoModel()):
                code, _, error = self.invoke(["--file", "input.diff", "--mode", "fix", "--format", "json", "--source-root", "."])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads((root / "output" / "fix-review.json").read_text())["status"], "proposed")
            self.assertIn("+    if not values:", (root / "output" / "repair.diff").read_text())
            self.assertIn("generated_tests/test_stats.py", (root / "output" / "tests.diff").read_text())
            self.assertEqual((root / "stats.py").read_text(), CURRENT)

    def test_root_commit_and_empty_staged_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout
            git("init", "-q")
            git("config", "user.name", "Example User")
            git("config", "user.email", "example@example.invalid")
            (root / "stats.py").write_text(CURRENT)
            git("add", "stats.py")
            git("-c", "commit.gpgsign=false", "commit", "-qm", "Initial fixture")
            original_run = subprocess.run
            def run_in_fixture(*args, **kwargs):
                return original_run(*args, cwd=root, **kwargs)
            with chdir(root), patch("src.cli.subprocess.run", side_effect=run_in_fixture):
                code, output, error = self.invoke(["--commit", "HEAD", "--scaffold"])
                self.assertEqual(code, 0, error)
                self.assertIn("Status: incomplete", output)
                code, _, error = self.invoke(["--scaffold"])
                self.assertEqual(code, 0)
                self.assertIn("Nothing to review", error)

    def test_source_root_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (Path(outside) / "outside.py").write_text("x=1\n")
            (root / "escape.py").symlink_to(Path(outside) / "outside.py")
            with self.assertRaises(ValueError):
                _read_sources(["escape.py"], str(root))

    def test_mismatched_source_fails_before_model_or_output_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.diff").write_text(SAMPLE_DIFF)
            (root / "stats.py").write_text(BEFORE)
            (root / "output").mkdir()
            (root / "output/review.md").write_text("previous report")
            with patch("src.llm.get_fast_llm") as initialize:
                code, _, error = self.invoke(["--file", str(root / "input.diff"), "--source-root", str(root)])
            self.assertEqual(code, 2)
            self.assertIn("post-change", error)
            self.assertNotIn("return sum", error)
            initialize.assert_not_called()
            self.assertEqual((root / "output/review.md").read_text(), "previous report")
            self.assertFalse((root / "output/runs").exists())

    def test_missing_configuration_error_is_clear_and_does_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.diff"
            path.write_text(SAMPLE_DIFF)
            with patch("src.llm.get_fast_llm", side_effect=ValueError("Set DEEPSEEK_API_KEY before running a model review")):
                code, _, error = self.invoke(["--file", str(path), "--run-root", str(Path(directory) / "runs")])
            self.assertEqual(code, 2)
            self.assertIn("DEEPSEEK_API_KEY", error)

    def test_provider_exception_is_suppressed_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.diff"
            path.write_text(SAMPLE_DIFF)
            with patch("src.llm.get_fast_llm", side_effect=RuntimeError("SENSITIVE_TEST_MARKER")):
                code, _, error = self.invoke(["--file", str(path), "--run-root", str(Path(directory) / "runs")])
            self.assertEqual(code, 1)
            self.assertNotIn("SENSITIVE_TEST_MARKER", error)


if __name__ == "__main__":
    unittest.main()
