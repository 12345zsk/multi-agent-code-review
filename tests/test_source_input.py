"""Whole-file review and guarded repair against explicit isolated source targets."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
from difflib import unified_diff
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from code_review import run_file_workflow
from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, unified
from src.cli import main
from src.diff import apply_patch, parse_diff, validate_source_context
from src.source_input import read_referenced_target, read_target, source_snapshot
from src.workflow.service import resume_workflow, run_workflow


class SourceModel(DemoModel):
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        self.calls.append((role, payload, messages[0].content))
        if role == "task_interpreter":
            assert payload["review_scope"] == "file"
            assert Path(payload["target_file"]).is_absolute()
            return AIMessage(content=json.dumps({
                "status": "accepted", "mode": "fix", "agents": None,
                "guidance": "审查并修正确认的缺陷，保留正常输入行为。",
                "write_policy": "required" if payload["writes_available"] else "optional",
                "unsupported_requests": [], "reason": "明确的指定源码文件修正任务。",
            }, ensure_ascii=False))
        return super().invoke(messages)


class SnapshotTests(unittest.TestCase):
    def test_explicit_sensitive_targets_are_rejected_before_opening(self):
        for relative in (".ssh/id_rsa", ".aws/credentials", ".azure/profile.json", ".kube/config",
                         ".npmrc", ".netrc", ".pypirc", "keys/client.pfx", "keys/client.ppk"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("SYNTHETIC_PRIVATE_FILE")
                with patch.object(Path, "open", side_effect=AssertionError("Sensitive file must not be opened")):
                    with self.assertRaisesRegex(ValueError, "sensitive"):
                        read_target(relative, root)

    def test_basename_lookup_skips_generated_tests_but_explicit_paths_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"examples").mkdir()
            (root/"generated_tests").mkdir()
            (root/"examples/test_stats.py").write_text(CURRENT)
            (root/"generated_tests/test_stats.py").write_text("# Generated regression artifact.\n")
            _, path, source = read_referenced_target("test_stats.py", root)
            self.assertEqual(path, "examples/test_stats.py")
            self.assertEqual(source, CURRENT)
            _, path, _ = read_referenced_target("generated_tests/test_stats.py", root)
            self.assertEqual(path, "generated_tests/test_stats.py")

    def test_snapshot_covers_current_file_without_inventing_a_change(self):
        for source in (CURRENT, CURRENT.rstrip("\n"), "", "\n", "x = 1\n\ny = 2"):
            with self.subTest(source=repr(source)):
                snapshot = source_snapshot("stats.py", source)
                changes = parse_diff(snapshot)
                self.assertEqual(changes[0].old_path, "stats.py")
                self.assertEqual(changes[0].new_path, "stats.py")
                self.assertTrue(all(kind == " " for h in changes[0].hunks for kind, _ in h.lines))
                self.assertEqual(apply_patch({"stats.py": source}, changes), {"stats.py": source})
                validate_source_context({"stats.py": source}, changes)

    def test_rejects_scope_widening_from_a_partial_diff(self):
        with self.assertRaisesRegex(ValueError, "complete, matching source snapshot"):
            run_workflow(SAMPLE_DIFF, model=SourceModel(), review_scope="file", file_contents={"stats.py": CURRENT})

    def test_paths_binary_and_size_are_rejected_before_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            outside = Path(other)/"outside.py"
            outside.write_text(CURRENT)
            (root/"linked.py").symlink_to(outside)
            for name in (".env", "private.pem", ".Git/config", ".venv/secret.py", "output/runs/demo/data.py"):
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("synthetic fixture")
            for name, content in (("binary.py", b"x\0y"), ("encoding.py", b"\xff"), ("large.py", b"x"*160001)):
                (root/name).write_bytes(content)
            for target in (outside, "../outside.py", "linked.py", "missing.py", ".env", "private.pem",
                           ".Git/config", ".venv/secret.py", "output/runs/demo/data.py", "binary.py", "encoding.py", "large.py"):
                with self.subTest(target=str(target)), patch("src.llm.get_fast_llm") as initialize:
                    with self.assertRaises(ValueError):
                        run_file_workflow(target, source_root=root)
                    initialize.assert_not_called()
            with self.assertRaises(ValueError):
                read_target("config", root/".Git")
            with self.assertRaises(ValueError):
                read_target("stats.py", root, run_root=root)


class SourceWorkflowTests(unittest.TestCase):
    def test_review_uses_existing_defect_scope_without_mutating_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            model = SourceModel()
            result = run_file_workflow("stats.py", source_root=root, model=model)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["review_scope"], "file")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertIn("Verdict on input source file", result["final_review"])
            self.assertNotIn("patch", result)
            for role, payload, prompt in model.calls:
                self.assertEqual(payload["review_scope"], "file")
                if role == "bug_detector":
                    self.assertIn("existing defects", prompt)
                    self.assertNotIn("Report only actionable issues introduced by this diff", prompt)
                if role == "adjudicator":
                    self.assertIn("existing defects", prompt)

    def test_fix_without_write_permission_only_returns_validated_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = run_file_workflow(root/"stats.py", source_root=root, mode="fix", model=SourceModel())
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["validation"].status, "passed")
            self.assertEqual(result["candidate_files"]["stats.py"], BEFORE)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertFalse((root/"tests").exists())
            self.assertFalse((root/"generated_tests").exists())

    def test_fix_writes_correct_target_with_original_byte_backup_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = CURRENT.replace("\n", "\r\n").encode()
            (root/"stats.py").write_bytes(original)
            (root/"stats.py").chmod(0o640)
            (root/"unrelated.py").write_text("unchanged\n")
            model = SourceModel()
            result = run_file_workflow(root/"stats.py", source_root=root, model=model, write_files=True,
                                       run_root=root/"output/runs", task=f"检查 {root/'stats.py'}，有错误就修正并写回。")
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["write_result"]["status"], "written")
            self.assertEqual((root/"stats.py").read_bytes(), BEFORE.replace("\n", "\r\n").encode())
            self.assertEqual((root/"stats.py").stat().st_mode & 0o777, 0o640)
            self.assertEqual((root/"unrelated.py").read_text(), "unchanged\n")
            entry = next(f for f in result["write_result"]["files"] if f["file_path"] == "stats.py")
            self.assertEqual((Path(result["run_dir"])/entry["backup"]).read_bytes(), original)
            self.assertEqual(result["runtime_validation"].status, "not_run")
            manifest = json.loads((Path(result["run_dir"])/"manifest.json").read_text())
            self.assertEqual(manifest["review_scope"], "file")
            self.assertEqual(model.calls[0][1]["target_file"], str(root/"stats.py"))

    def test_syntax_error_repair_writes_with_missing_eof_marker(self):
        source = 'import\n\ndef main():\n    """Return one."""\n    return 1 / 0'
        repaired = 'def main():\n    """Return one."""\n    return 1\n'

        class SyntaxModel(SourceModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0]
                payload = json.loads(messages[1].content)
                if role == "ROLE: bug_detector":
                    return AIMessage(content=json.dumps({"summary": "Syntax and explicit return contract defects.", "findings": [{
                        "category": "other", "title": "Invalid function module", "description": "Incomplete import and zero division violate the visible module contract.",
                        "file_path": "target.py", "line": 1, "severity": "high", "fixable": True,
                        "suggestion": "Remove the incomplete import and return one.", "evidence": source,
                    }]}))
                if role == "ROLE: patch_generator":
                    # Simulate model output omitting the original EOF marker.
                    diff = "".join(line if line.endswith("\n") else line+"\n\\ No newline at end of file\n"
                                   for line in unified_diff(source.splitlines(keepends=True), repaired.splitlines(keepends=True),
                                                            fromfile="a/target.py", tofile="b/target.py"))
                    diff = diff.replace("\\ No newline at end of file\n", "")
                    return AIMessage(content=json.dumps({"finding_ids": payload["plan"]["finding_ids"], "diff": diff, "summary": "Restore valid module."}))
                if role == "ROLE: test_generator":
                    return AIMessage(content=json.dumps({"status": "not_needed", "diff": "", "summary": "Synthetic syntax fixture; runtime verification remains external."}))
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"target.py").write_text(source)
            result = run_file_workflow("target.py", source_root=root, mode="fix", model=SyntaxModel(),
                                       write_files=True, run_root=root/"runs")
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["validation"].status, "passed")
            self.assertEqual((root/"target.py").read_text(), repaired)
            self.assertEqual(result["fix_attempts"], 1)
            self.assertEqual(result["write_result"]["status"], "written")
            entry = next(f for f in result["write_result"]["files"] if f["file_path"] == "target.py")
            self.assertEqual((Path(result["run_dir"])/entry["backup"]).read_bytes(), source.encode())
            compile(repaired, "target.py", "exec")

    def test_invalid_candidate_and_concurrent_edits_are_not_overwritten(self):
        for failure in ("syntax", "concurrent", "scope"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                (root/"unrelated.py").write_text("unchanged\n")

                class Failing(SourceModel):
                    def invoke(self, messages):
                        reply = super().invoke(messages)
                        if messages[0].content.startswith("ROLE: patch_generator\n"):
                            data = json.loads(reply.content)
                            if failure == "syntax":
                                data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
                            if failure == "scope":
                                data["diff"] = data["diff"].replace("stats.py", "unrelated.py")
                            reply = AIMessage(content=json.dumps(data))
                        if failure == "concurrent" and messages[0].content.startswith("ROLE: regression_reviewer\n"):
                            (root/"stats.py").write_text("user edit\n")
                        return reply

                result = run_file_workflow("stats.py", source_root=root, model=Failing(), task="检查并修复这个文件。",
                                           run_root=root/"runs", max_fix_attempts=1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual((root/"stats.py").read_text(), "user edit\n" if failure == "concurrent" else CURRENT)
                self.assertEqual((root/"unrelated.py").read_text(), "unchanged\n")
                self.assertFalse((Path(result["run_dir"])/"writes").exists())

    def test_resume_reuses_file_snapshot_and_completed_task_interpretation(self):
        class Interrupted(SourceModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    raise ConnectionError("Synthetic specialist interruption")
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            failed = run_file_workflow("stats.py", source_root=root, model=Interrupted(), task="检查并修正这个文件，写回本地。",
                                       run_root=root/"runs")
            self.assertEqual(failed["status"], "failed")
            model = SourceModel()
            resumed = resume_workflow(failed["run_id"], run_root=root/"runs", model=model)
            self.assertEqual(resumed["status"], "proposed")
            self.assertEqual(resumed["review_scope"], "file")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertNotIn("task_interpreter", [role for role, _, _ in model.calls])

    def test_conflicting_natural_language_target_stops_without_writing_either_file(self):
        class WrongTarget(SourceModel):
            def invoke(self, messages):
                self.calls.append(("task_interpreter", {}, ""))
                return AIMessage(content=json.dumps({
                    "status": "needs_clarification", "mode": None, "agents": None, "guidance": None,
                    "write_policy": None, "unsupported_requests": [],
                    "reason": "请求的 other.py 与明确提供的 stats.py 不一致，请明确目标。",
                }))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"other.py").write_text("keep user content\n")
            model = WrongTarget()
            result = run_file_workflow("stats.py", source_root=root, model=model, task="修正 other.py 并写回。",
                                       write_files=True, run_root=root/"runs")
            self.assertEqual(result["status"], "needs_clarification")
            self.assertEqual(result["reports"], {})
            self.assertEqual(len(model.calls), 1)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertEqual((root/"other.py").read_text(), "keep user content\n")


class SourceCliTests(unittest.TestCase):
    def invoke(self, root, args, model):
        out, err = StringIO(), StringIO()
        with chdir(root), patch("sys.argv", ["code-review", *args]), patch("src.llm.get_fast_llm", return_value=model), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                status = 0
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def test_target_works_without_git_or_prepared_diff_and_defaults_root_to_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            code, out, err = self.invoke(root, ["--target", "stats.py", "--task", "检查并修正确认的缺陷。"], SourceModel())
            self.assertEqual(code, 0, err)
            self.assertIn("Review scope: full source file", out)
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertTrue((root/"output/fix-review.md").is_file())

    def test_absolute_target_and_json_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            code, _, err = self.invoke(root, ["--target", str(root/"stats.py"), "--source-root", str(root), "--mode", "fix", "--format", "json"], SourceModel())
            self.assertEqual(code, 0, err)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            report = json.loads((root/"output/fix-review.json").read_text())
            self.assertEqual(report["review_scope"], "file")
            self.assertEqual(report["file_paths"], ["stats.py"])

    def test_target_and_unrelated_diff_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = SourceModel()
            code, _, _ = self.invoke(root, ["--file", "old.diff", "--target", "stats.py"], model)
            self.assertEqual(code, 2)
            self.assertEqual(model.calls, [])

    def test_readonly_report_or_patch_export_cannot_clobber_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for flag in ("--output", "--patch-output", "--test-output"):
                with self.subTest(flag=flag):
                    (root/"stats.py").write_text(CURRENT)
                    code, _, err = self.invoke(root, ["--target", "stats.py", flag, "stats.py"], SourceModel())
                    self.assertEqual(code, 2, err)
                    self.assertIn("must not overwrite", err)
                    self.assertEqual((root/"stats.py").read_text(), CURRENT)


if __name__ == "__main__":
    unittest.main()
