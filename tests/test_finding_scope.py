"""Recover model scope mistakes without expanding review or write permissions."""

import json
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import AIMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF
from src.workflow.service import resume_workflow, run_workflow


class ScopeModel(DemoModel):
    def __init__(self, paths, *, correction="omit", keep_valid=True):
        self.paths, self.correction, self.keep_valid = paths, correction, keep_valid
        self.scope_calls = []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        response = super().invoke(messages)
        if role != "bug_detector":
            return response
        self.scope_calls.append(payload)
        data = json.loads(response.content)
        valid = data["findings"][0]
        if "scope_feedback" in payload:
            if self.correction == "omit":
                data["findings"] = []
            elif self.correction == "correct":
                data["findings"] = [valid]
            elif self.correction == "bad_evidence":
                data["findings"] = [{**valid, "evidence": "unsupported evidence"}]
            else:
                data["findings"] = [{**valid, "file_path": path} for path in self.paths]
        else:
            data["findings"] = ([valid] if self.keep_valid else []) + [
                {**valid, "file_path": path, "title": "Context-only observation"} for path in self.paths
            ]
        return AIMessage(content=json.dumps(data))


class FindingScopeTests(unittest.TestCase):
    def execute(self, root, model, **options):
        return run_workflow(SAMPLE_DIFF, mode="fix", model=model, file_contents={"stats.py": CURRENT},
                            run_root=root/"runs", **options)

    def test_context_finding_correction_preserves_valid_fix_and_read_only_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"tests").mkdir()
            existing = root/"tests/test_smoke.py"
            existing.write_text("# Existing context-only tests\n")
            model = ScopeModel(["tests/test_smoke.py"])
            result = self.execute(root, model, source_root=root, write_root=root)
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["write_result"]["status"], "written")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertEqual(existing.read_text(), "# Existing context-only tests\n")
            self.assertEqual(len(model.scope_calls), 2)
            self.assertEqual(len(result["reports"]["bug_detector"].findings), 1)
            self.assertTrue(any(path.startswith("generated_tests/") for path in result["candidate_files"]))
            self.assertEqual(result["file_paths"], ["stats.py"])

    def test_retry_can_correct_a_path_using_original_target_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ScopeModel(["tests/test_smoke.py"], correction="correct", keep_valid=False)
            result = self.execute(Path(directory), model)
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(result["findings"][0].file_path, "stats.py")
            self.assertEqual(model.scope_calls[1]["scope_feedback"]["allowed_file_paths"], ["stats.py"])
            self.assertEqual(model.scope_calls[1]["sources"]["stats.py"], CURRENT)
            self.assertTrue(any(event["event"] == "finding_scope_retry" for event in result["trace_events"]))

    def test_repeated_unsafe_paths_are_excluded_with_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ScopeModel(["tests/test_smoke.py", "../outside.py", "/tmp/outside.py"], correction="repeat")
            result = self.execute(Path(directory), model)
            self.assertEqual(result["status"], "proposed")
            self.assertEqual(len(model.scope_calls), 2)
            self.assertEqual({f.file_path for f in result["findings"]}, {"stats.py"})
            event = next(event for event in result["trace_events"] if event["event"] == "finding_scope_rejected")
            self.assertEqual(event["rejected_count"], 3)
            self.assertNotIn("/tmp/outside.py", json.dumps(result["trace_events"]))
            self.assertIn("3 out-of-scope finding(s) excluded", result["final_review"])

    def test_correction_does_not_bypass_evidence_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ScopeModel(["tests/test_smoke.py"], correction="bad_evidence", keep_valid=False)
            result = self.execute(Path(directory), model)
            self.assertEqual(len(model.scope_calls), 2)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["fix_attempts"], 0)
            self.assertEqual(result["write_result"]["status"], "not_run")
            self.assertTrue(any(item["stage"] == "finding_evidence" for item in result["diagnostics"]))

    def test_only_rejected_context_findings_do_not_claim_a_clean_review(self):
        with tempfile.TemporaryDirectory() as directory:
            model = ScopeModel(["tests/test_smoke.py"], correction="repeat", keep_valid=False)
            result = self.execute(Path(directory), model)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["fix_attempts"], 0)
            self.assertEqual(result["findings"], [])
            self.assertIn("NEEDS DISCUSSION", result["final_review"])
            self.assertNotIn("APPROVE", result["final_review"])

    def test_failed_correction_resumes_from_the_saved_react_answer(self):
        class InterruptedCorrection(ScopeModel):
            def invoke(self, messages):
                payload = json.loads(messages[1].content)
                if "scope_feedback" in payload:
                    raise ConnectionError("Synthetic scope-correction interruption")
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = InterruptedCorrection(["tests/test_smoke.py"])
            failed = self.execute(root, initial)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["fix_attempts"], 0)
            resumed_model = ScopeModel(["tests/test_smoke.py"])
            resumed = resume_workflow(failed["run_id"], run_root=root/"runs", model=resumed_model)
            self.assertEqual(resumed["status"], "proposed")
            self.assertEqual(len(resumed_model.scope_calls), 1)
            self.assertIn("scope_feedback", resumed_model.scope_calls[0])
            self.assertEqual(len(resumed["reports"]["bug_detector"].findings), 1)
            self.assertFalse(any(item["node"] == "bug_detector" and not item["resolved"]
                                 for item in resumed["diagnostics"]))
