"""Standalone natural-language requests resolve one target and repair it in place."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from code_review import run_task_workflow
from examples.repair_demo import BEFORE, CURRENT, DemoModel, unified
from src.cli import main
from src.source_input import explicit_reference
from src.workflow.service import resume_workflow


class RequestModel(DemoModel):
    def __init__(self, target="stats.py", *, mode="fix", policy="required", outcome="accepted"):
        self.target, self.mode, self.policy, self.outcome = target, mode, policy, outcome
        self.calls = []
        self.path = "stats.py"

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        self.calls.append((role, payload))
        if role == "task_interpreter":
            assert payload["resolve_target"]
            assert payload["file_paths"] == []
            admitted = self.outcome == "accepted"
            return AIMessage(content=json.dumps({
                "status": self.outcome, "mode": self.mode if admitted else None,
                "agents": None,
                "guidance": "检查指定源码，遵守空输入返回零的契约。" if admitted else None,
                "write_policy": self.policy if admitted else None,
                "target": self.target if admitted else None,
                "unsupported_requests": ["部署上线"] if self.outcome == "unsupported" else [],
                "reason": "明确的单文件任务。" if admitted else "无法执行，请明确一个支持的单文件审查或修正目标。",
            }, ensure_ascii=False))
        if role == "bug_detector":
            self.path = payload["file_paths"][0]
        data = json.loads(super().invoke(messages).content)
        if role == "bug_detector":
            data["findings"][0]["file_path"] = self.path
        elif role == "patch_generator":
            data["diff"] = unified(CURRENT, BEFORE, self.path)
        elif role == "test_generator" and self.path != "stats.py":
            data = {"status": "not_needed", "diff": "", "summary": "Synthetic nested-path fixture; runtime verification is external."}
        return AIMessage(content=json.dumps(data))


class NaturalRequestTests(unittest.TestCase):
    def execute(self, root, model, task):
        return run_task_workflow(task, source_root=root, run_root=root/"runs", model=model)

    def test_natural_request_writes_original_and_saves_resolved_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            model = RequestModel()
            result = self.execute(root, model, "检查 stats.py，如果有错误就修正。")
            self.assertEqual(result["status"], "proposed")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertEqual(result["file_paths"], ["stats.py"])
            self.assertEqual(result["target_file"], str(root.resolve()/"stats.py"))
            entry = next(item for item in result["write_result"]["files"] if item["file_path"] == "stats.py")
            self.assertEqual((Path(result["run_dir"])/entry["backup"]).read_text(), CURRENT)
            self.assertFalse((root/"stats_fixed.py").exists())
            self.assertEqual(sum(role == "task_interpreter" for role, _ in model.calls), 1)
            inputs = json.loads((Path(result["run_dir"])/"input.json").read_text())
            self.assertEqual(inputs["code_diff"], "")
            self.assertEqual(inputs["file_contents"], {})
            self.assertTrue(any(event.get("stage") == "target_resolution" for event in result["trace_events"]))
            manifest = json.loads((Path(result["run_dir"])/"manifest.json").read_text())
            self.assertEqual(manifest["target_file"], result["target_file"])

    def test_unique_basename_resolves_nested_file_and_full_path_handles_spaces(self):
        for reference, relative in (("stats.py", "examples/stats.py"), ("examples/my file.py", "examples/my file.py")):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root/relative
                target.parent.mkdir()
                target.write_text(CURRENT)
                result = self.execute(root, RequestModel(reference), f"检查 {reference} 并修正错误。")
                self.assertEqual(result["status"], "proposed")
                self.assertEqual(result["file_paths"], [relative])
                self.assertEqual(target.read_text(), BEFORE)

    def test_absolute_path_is_resolved_without_a_target_option(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/"stats.py"
            target.write_text(CURRENT)
            result = self.execute(root, RequestModel(str(target)), f"检查{target}这个代码，有错误就修正。")
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(target.read_text(), BEFORE)

    def test_ambiguous_or_missing_filename_does_not_choose_another_file(self):
        for reference in ("stats.py", "missing.py"):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                (root/"examples").mkdir()
                (root/"examples/stats.py").write_text(CURRENT)
                model = RequestModel(reference)
                result = self.execute(root, model, f"修正 {reference}。")
                self.assertEqual(result["status"], "needs_clarification")
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(result["reports"], {})
                self.assertEqual((root/"stats.py").read_text(), CURRENT)
                self.assertEqual((root/"examples/stats.py").read_text(), CURRENT)

    def test_model_cannot_invent_or_shorten_a_requested_path(self):
        for task in ("检查 other.py 并修正。", "修正 /outside/project/stats.py。", "修正 mystats.py。"):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                model = RequestModel("stats.py")
                result = self.execute(root, model, task)
                self.assertEqual(result["status"], "needs_clarification")
                self.assertEqual(len(model.calls), 1)
                self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_protected_symlink_and_external_paths_are_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory)
            outside = Path(other)/"stats.py"
            outside.write_text(CURRENT)
            (root/"stats.py").write_text(CURRENT)
            (root/"linked.py").symlink_to(outside)
            (root/".env").write_text('password="SYNTHETIC_NATURAL_SECRET"\n')
            for reference in (str(outside), "./linked.py", ".env", "../stats.py"):
                with self.subTest(reference=reference):
                    result = self.execute(root, RequestModel(reference), f"检查 {reference}，有错误就修正。")
                    self.assertEqual(result["status"], "needs_clarification")
                    self.assertEqual(result["reports"], {})
                    self.assertNotIn("SYNTHETIC_NATURAL_SECRET", repr(result))
                    self.assertEqual(outside.read_text(), CURRENT)
                    self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_missing_target_and_unsupported_goals_stop_before_reading_source(self):
        for outcome, task in (("needs_clarification", "帮我处理一下"),
                              ("needs_clarification", "修正 stats.py 和 other.py"),
                              ("unsupported", "审查 stats.py 并部署上线")):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = RequestModel(outcome=outcome)
                with patch("src.source_input.read_referenced_target", side_effect=AssertionError("No source read expected")):
                    result = self.execute(root, model, task)
                self.assertEqual(result["status"], outcome)
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(result["reports"], {})

    def test_review_and_candidate_only_requests_leave_original_unchanged(self):
        for mode, policy, task in (("review", "optional", "只审查 stats.py。"),
                                   ("fix", "forbidden", "修复 stats.py，只生成补丁，不修改文件。")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                result = self.execute(root, RequestModel(mode=mode, policy=policy), task)
                self.assertIn(result["status"], {"completed", "proposed"})
                self.assertIsNone(result["write_root"])
                self.assertEqual((root/"stats.py").read_text(), CURRENT)
                self.assertFalse((Path(result["run_dir"])/"writes").exists())

    def test_completed_resolution_and_snapshot_are_reused_on_resume(self):
        class Interrupted(RequestModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: patch_generator\n"):
                    raise ConnectionError("Synthetic repair interruption")
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            failed = self.execute(root, Interrupted(), "检查 stats.py，有错误就修正。")
            self.assertEqual(failed["status"], "failed")
            (root/"new").mkdir()
            (root/"new/stats.py").write_text("unrelated newly added file\n")
            model = RequestModel()
            resumed = resume_workflow(failed["run_id"], run_root=root/"runs", model=model)
            self.assertEqual(resumed["status"], "proposed")
            self.assertNotIn("task_interpreter", [role for role, _ in model.calls])
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertEqual((root/"new/stats.py").read_text(), "unrelated newly added file\n")
            self.assertEqual(resumed["source_hashes"], failed["source_hashes"])

    def test_uncommitted_interpretation_failure_can_resume(self):
        class Interrupted(RequestModel):
            def invoke(self, messages):
                raise ConnectionError("PRIVATE_REQUEST_FAILURE_MARKER")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            failed = self.execute(root, Interrupted(), "修正 stats.py。")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["file_paths"], [])
            for file in Path(failed["run_dir"]).rglob("*"):
                if file.is_file():
                    self.assertNotIn(b"PRIVATE_REQUEST_FAILURE_MARKER", file.read_bytes())
            resumed = resume_workflow(failed["run_id"], run_root=root/"runs", model=RequestModel())
            self.assertEqual(resumed["status"], "proposed")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)

    def test_missing_or_empty_checkpoint_cannot_select_the_target_again(self):
        class Interrupted(RequestModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: patch_generator\n"):
                    raise ConnectionError("Synthetic repair interruption")
                return super().invoke(messages)

        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                failed = self.execute(root, Interrupted(), "修正 stats.py。")
                self.assertEqual(failed["status"], "failed")
                checkpoint = Path(failed["run_dir"])/"checkpoint.sqlite"
                checkpoint.unlink()
                model = RequestModel()
                if missing:
                    with self.assertRaisesRegex(ValueError, "checkpoint is missing"):
                        resume_workflow(failed["run_id"], run_root=root/"runs", model=model)
                else:
                    checkpoint.touch()
                    for _ in range(2):
                        resumed = resume_workflow(failed["run_id"], run_root=root/"runs", model=model)
                        self.assertEqual(resumed["status"], "failed")
                        self.assertTrue(any(item["error_code"] == "REQUEST_SNAPSHOT_MISSING" for item in resumed["diagnostics"]))
                self.assertEqual(model.calls, [])
                self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_search_budget_requires_an_explicit_path_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            with patch("src.source_input.MAX_SEARCH_ENTRIES", 1):
                result = self.execute(root, RequestModel(), "修正 stats.py。")
            self.assertEqual(result["status"], "needs_clarification")
            self.assertIn("完整相对路径", result["final_review"])
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_reference_boundaries_allow_prose_but_not_other_path_suffixes(self):
        for task in ("检查stats.py这个文件", "Review stats.py.", "请检查 `stats.py`。"):
            self.assertTrue(explicit_reference(task, "stats.py"))
        for task in ("修正 /outside/stats.py。", "修正 mystats.py。", "修正 stats.pyx。"):
            self.assertFalse(explicit_reference(task, "stats.py"))


class NaturalCliTests(unittest.TestCase):
    def invoke(self, root, model, args):
        out, err = StringIO(), StringIO()
        with chdir(root), patch("sys.argv", ["code-review", *args]), patch("src.llm.get_fast_llm", return_value=model), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_plain_sentence_and_task_option_work_without_target_diff_or_git(self):
        for args in (["检查 stats.py，有错误就修正。"], ["--task", "检查 stats.py，有错误就修正。"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                with patch("src.cli._get_diff_and_files", side_effect=AssertionError("No Git/default diff fallback")):
                    code, out, err = self.invoke(root, RequestModel(), args)
                self.assertEqual(code, 0, err)
                self.assertEqual((root/"stats.py").read_text(), BEFORE)
                self.assertIn("Written:", err)
                self.assertIn("Status: written", out)
                self.assertTrue((root/"output/fix-review.md").is_file())

    def test_resolved_readonly_target_cannot_be_overwritten_by_report_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            code, _, err = self.invoke(root, RequestModel(mode="review", policy="optional"), ["只审查 stats.py。", "--output", "stats.py"])
            self.assertEqual(code, 2)
            self.assertIn("must not overwrite", err)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_conflicting_request_forms_stop_before_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = RequestModel()
            code, _, _ = self.invoke(root, model, ["修正 stats.py。", "--task", "只审查 other.py。"])
            self.assertEqual(code, 2)
            self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
