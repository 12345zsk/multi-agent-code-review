"""Guarded local writes, backups and recovery, using only isolated fixtures."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, TESTS, unified
from src.cli import main
from src.diff import parse_diff
from src.file_tools import CandidateWriter, FileWriteError, _replace
from src.workflow.service import resume_workflow, run_workflow


class FileToolTests(unittest.TestCase):
    def writer(self, root, *, candidates=None, sources=None):
        run_dir = root/"output/runs/example"
        run_dir.mkdir(parents=True, exist_ok=True)
        return CandidateWriter(root, sources={"stats.py": CURRENT} if sources is None else sources,
                               candidates={"stats.py": BEFORE} if candidates is None else candidates, run_dir=run_dir)

    def test_write_preserves_backup_mode_and_receipt_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/"stats.py"
            target.write_text(CURRENT)
            target.chmod(0o640)
            writer = self.writer(root)
            result = writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})
            backup = writer.run_dir/result["backup"]
            self.assertEqual(target.read_text(), BEFORE)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(backup.read_text(), CURRENT)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})["status"], "already_written")
            self.assertEqual(backup.read_text(), CURRENT)

    def test_preserves_crlf_and_original_bytes_in_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_bytes(CURRENT.replace("\n", "\r\n").encode())
            writer = self.writer(root)
            result = writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})
            self.assertEqual((root/"stats.py").read_bytes(), BEFORE.replace("\n", "\r\n").encode())
            self.assertEqual((writer.run_dir/result["backup"]).read_bytes(), CURRENT.replace("\n", "\r\n").encode())
            self.assertEqual(writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})["status"], "already_written")

    def test_creates_only_approved_new_file_and_rejects_existing_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = self.writer(root, sources={}, candidates={"tests/test_stats.py": TESTS})
            (root/"tests").mkdir()
            target = root/"tests/test_stats.py"
            target.write_text("existing user tests\n")
            with self.assertRaisesRegex(FileWriteError, "WRITE_NEW_FILE_EXISTS"):
                writer.preflight()
            self.assertEqual(target.read_text(), "existing user tests\n")
            target.unlink()
            result = writer.tool.invoke({"file_path": "tests/test_stats.py", "content": TESTS})
            self.assertEqual(target.read_text(), TESTS)
            self.assertIsNone(result["backup"])

    def test_changed_source_and_unapproved_content_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text("user edit\n")
            writer = self.writer(root)
            for path, content in (("stats.py", BEFORE), ("stats.py", "unapproved\n"), ("other.py", BEFORE)):
                with self.subTest(path=path, content=content), self.assertRaises(FileWriteError):
                    writer.tool.invoke({"file_path": path, "content": content})
            self.assertEqual((root/"stats.py").read_text(), "user edit\n")
            self.assertFalse(writer.directory.exists())

    def test_paths_cannot_escape_or_write_sensitive_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("../outside.py", "/tmp/outside.py", ".git/config", ".Git/config", ".env", "private.pem", "output/runs/example/checkpoint.sqlite"):
                with self.subTest(path=path):
                    writer = self.writer(root, sources={}, candidates={path: "safe text\n"})
                    with self.assertRaises(FileWriteError):
                        writer.tool.invoke({"file_path": path, "content": "safe text\n"})
            (root/"real").mkdir()
            (root/"link").symlink_to(root/"real", target_is_directory=True)
            writer = self.writer(root, sources={}, candidates={"link/new.py": "safe text\n"})
            with self.assertRaises(FileWriteError):
                writer.tool.invoke({"file_path": "link/new.py", "content": "safe text\n"})
            self.assertFalse((root/"real/new.py").exists())

    def test_redacted_candidate_and_corrupt_backup_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            writer = self.writer(root, candidates={"stats.py": "# <REDACTED>\n"})
            with self.assertRaises(FileWriteError):
                writer.tool.invoke({"file_path": "stats.py", "content": "# <REDACTED>\n"})
            writer = self.writer(root)
            result = writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})
            (writer.run_dir/result["backup"]).write_text("corrupt backup\n")
            with self.assertRaises(FileWriteError):
                writer.tool.invoke({"file_path": "stats.py", "content": BEFORE})
            self.assertEqual((root/"stats.py").read_text(), BEFORE)


class WriteWorkflowTests(unittest.TestCase):
    def execute(self, root, model=None, **kwargs):
        return run_workflow(SAMPLE_DIFF, mode="fix", model=model or DemoModel(),
                            file_contents={"stats.py": CURRENT}, run_root=root/"output/runs", write_root=root, **kwargs)

    def test_validated_candidates_are_written_and_reported_with_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = self.execute(root)
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["write_result"]["status"], "written")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            test_path = parse_diff(result["test_patch"].diff)[0].path
            self.assertRegex(test_path, r"^generated_tests/test_stats__run_[a-f0-9]{12}\.py$")
            self.assertEqual((root/test_path).read_text(), TESTS)
            self.assertEqual(result["runtime_validation"].status, "not_run")
            self.assertNotIn("repository files were not changed", result["final_review"])
            manifest = json.loads((Path(result["run_dir"])/"manifest.json").read_text())
            self.assertTrue(any(item["classification"] == "source_backup" for item in manifest["artifacts"].values()))
            events = [item for item in result["trace_events"] if item["event"] == "tool_completed" and item.get("tool") == "write_file"]
            self.assertEqual(len(events), 2)

    def test_failed_validation_or_unsupported_deletion_never_writes(self):
        class Rejected(DemoModel):
            def __init__(self, failure):
                self.failure = failure
            def invoke(self, messages):
                reply = super().invoke(messages)
                data = json.loads(reply.content)
                role = messages[0].content.splitlines()[0]
                if self.failure == "static" and role == "ROLE: patch_generator":
                    data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
                if self.failure == "model" and role == "ROLE: regression_reviewer":
                    data.update(status="failed", summary="Candidate remains incorrect.")
                if self.failure == "deletion" and role == "ROLE: patch_generator":
                    data["diff"] = unified(CURRENT, "", "stats.py").replace("+++ b/stats.py", "+++ /dev/null")
                return AIMessage(content=json.dumps(data))
        for failure in ("static", "model", "deletion"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                result = self.execute(root, Rejected(failure), max_fix_attempts=1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual((root/"stats.py").read_text(), CURRENT)
                self.assertFalse((root/"tests").exists())
                self.assertFalse((root/"generated_tests").exists())
                self.assertFalse((Path(result["run_dir"])/"writes").exists())

    def test_preflight_checks_all_targets_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            collisions = []
            class ConcurrentTest(DemoModel):
                def invoke(self, messages):
                    if messages[0].content.startswith("ROLE: regression_reviewer\n"):
                        payload = json.loads(messages[1].content)
                        path = next(p for p in payload["candidate_sources"] if p.startswith("generated_tests/"))
                        (root/path).parent.mkdir(parents=True, exist_ok=True)
                        (root/path).write_text("existing user tests\n")
                        collisions.append(path)
                    return super().invoke(messages)
            result = self.execute(root, ConcurrentTest())
            self.assertEqual(result["status"], "failed")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertEqual((root/collisions[0]).read_text(), "existing user tests\n")
            self.assertTrue(any(item["error_code"] == "WRITE_NEW_FILE_EXISTS" for item in result["diagnostics"]))
            self.assertFalse((Path(result["run_dir"])/"writes").exists())

    def test_repeated_runs_preserve_existing_tests_and_use_distinct_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"tests").mkdir()
            existing = root/"tests/test_stats.py"
            original = b"# Existing user regression tests must be preserved.\n"
            existing.write_bytes(original)
            paths = []
            for _ in range(2):
                (root/"stats.py").write_text(CURRENT)
                result = self.execute(root)
                self.assertEqual(result["status"], "proposed")
                self.assertEqual(result["write_result"]["status"], "written")
                self.assertEqual((root/"stats.py").read_text(), BEFORE)
                self.assertEqual(existing.read_bytes(), original)
                self.assertEqual(list((root/"tests").iterdir()), [existing])
                path = parse_diff(result["test_patch"].diff)[0].path
                self.assertEqual((root/path).read_text(), TESTS)
                self.assertIn(path, result["final_review"])
                self.assertTrue(any(item["file_path"] == path for item in result["write_result"]["files"]))
                paths.append(path)
            self.assertNotEqual(paths[0], paths[1])
            self.assertEqual((root/paths[0]).read_text(), TESTS)

    def test_partial_write_resume_reuses_receipt_and_keeps_original_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            def fail_source_write(path, data, mode=0o600):
                if path == (root/"stats.py").resolve():
                    raise OSError("Synthetic write failure")
                return _replace(path, data, mode)
            with patch("src.file_tools._replace", side_effect=fail_source_write):
                failed = self.execute(root)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["write_result"]["status"], "partial")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            test_path = parse_diff(failed["test_patch"].diff)[0].path
            self.assertEqual((root/test_path).read_text(), TESTS)
            resumed = resume_workflow(failed["run_id"], run_root=root/"output/runs", model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            self.assertEqual(parse_diff(resumed["test_patch"].diff)[0].path, test_path)
            self.assertEqual((root/test_path).read_text(), TESTS)
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            test_entry = next(item for item in resumed["write_result"]["files"] if item["file_path"] == test_path)
            self.assertEqual(test_entry["status"], "already_written")
            entry = next(item for item in resumed["write_result"]["files"] if item["file_path"] == "stats.py")
            self.assertEqual(entry["status"], "written")
            self.assertEqual((Path(resumed["run_dir"])/entry["backup"]).read_text(), CURRENT)
            starts = [item["node"] for item in resumed["trace_events"] if item["event"] == "node_started"]
            self.assertEqual(starts.count("patch_generator"), 1)

    def test_source_edit_after_review_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            class ConcurrentEdit(DemoModel):
                def invoke(self, messages):
                    if messages[0].content.startswith("ROLE: regression_reviewer\n"):
                        (root/"stats.py").write_text("user edit while reviewing\n")
                    return super().invoke(messages)
            result = self.execute(root, ConcurrentEdit())
            self.assertEqual(result["status"], "failed")
            self.assertEqual((root/"stats.py").read_text(), "user edit while reviewing\n")
            self.assertFalse((root/"tests").exists())
            self.assertFalse((root/"generated_tests").exists())
            self.assertTrue(any(item["error_code"] == "FILE_VERSION_CHANGED" for item in result["diagnostics"]))

    def test_write_requires_explicit_fix_mode_and_persistent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            for options in ({"mode": "review", "run_root": directory}, {"mode": "fix"},
                            {"mode": "fix", "run_root": directory, "scaffold": True}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    run_workflow(SAMPLE_DIFF, write_root=directory, **options)

    def test_cli_write_files_updates_local_source_and_exports_report(self):
        class NativeReadModel(DemoModel):
            def bind_tools(self, tools, **kwargs):
                assert {item.name for item in tools} == {"read_source", "list_project_files", "search_code", "analyze_python"}
                return self
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n") and not any(isinstance(item, ToolMessage) for item in messages):
                    return AIMessage(content="", tool_calls=[{"name": "read_source", "args": {"file_path": "stats.py"}, "id": "read-1", "type": "tool_call"}])
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            with chdir(root), patch("sys.argv", ["code-review", "--file", "input.diff", "--source-root", ".", "--mode", "fix", "--write-files"]), patch(
                "src.llm.get_fast_llm", return_value=NativeReadModel()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main()
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertIn("Status: written", (root/"output/fix-review.md").read_text())

    def test_report_export_cannot_overwrite_written_candidate(self):
        from src.runtime.store import RunStore, publish_run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = self.execute(root)
            store = RunStore.open(root/"output/runs", result["run_id"])
            with self.assertRaisesRegex(ValueError, "repair targets"):
                publish_run(store, report_path=root/"stats.py", patch_path=root/"output/repair.diff",
                            test_path=root/"output/tests.diff", output_root=root/"output")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)


if __name__ == "__main__":
    unittest.main()
