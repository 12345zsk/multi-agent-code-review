"""Structured labels, one-to-one detection metrics and verified repair scoring."""

from copy import deepcopy
from contextlib import chdir, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.repair_demo import BEFORE, CURRENT, unified
from evals.cases import load_cases
from evals.scoring import aggregate, annotation_template, score_report
from evals.run_eval import run


LABELS = {"case_id": "sql_example", "expected": [{"id": "sql", "category": "security.sql_injection",
    "file_path": "api.py", "line_start": 3, "line_end": 4}]}


def prediction(id="f-1", *, status="confirmed", category="security.sql_injection", line=3, path="api.py"):
    return {"id": id, "category": category, "status": status, "file_path": path, "line": line}


def report(findings=None, *, status="completed"):
    return {"run_id": "test-run", "status": status, "findings": [prediction()] if findings is None else findings}


class EvaluationTests(unittest.TestCase):
    def test_verdict_and_negative_keyword_mentions_cannot_pass_detection(self):
        state = report([])
        state["final_review"] = "APPROVE: No SQL injection issue found."
        result = score_report(state, LABELS)
        self.assertEqual(result["metrics"]["true_positives"], 0)
        self.assertEqual(result["metrics"]["false_negatives"], 1)
        self.assertEqual(result["metrics"]["recall"], 0)

    def test_candidate_and_rejected_findings_are_not_confirmed_detections(self):
        result = score_report(report([prediction(status="candidate"), prediction("rejected", status="rejected")]), LABELS)
        self.assertEqual(result["metrics"]["true_positives"], 0)
        self.assertEqual(result["metrics"]["false_negatives"], 1)
        self.assertEqual(result["unconfirmed_count"], 1)

    def test_duplicate_predictions_count_as_false_positives(self):
        result = score_report(report([prediction(), prediction("f-2")]), LABELS)
        self.assertEqual(result["metrics"]["true_positives"], 1)
        self.assertEqual(result["metrics"]["false_positives"], 1)
        self.assertEqual(result["metrics"]["precision"], 0.5)
        self.assertEqual(result["metrics"]["false_discovery_rate"], 0.5)

    def test_location_is_assessed_separately_from_detection(self):
        result = score_report(report([prediction(line=90)]), LABELS)
        self.assertEqual(result["metrics"]["recall"], 1)
        self.assertEqual(result["metrics"]["localization_accuracy"], 0)
        missing = score_report(report([prediction(line=None)]), LABELS)
        self.assertEqual(missing["metrics"]["localization_accuracy"], 0)

    def test_wrong_category_or_file_is_not_a_true_positive(self):
        result = score_report(report([prediction(path="other.py"), prediction("f-2", category="other")]), LABELS)
        self.assertEqual(result["metrics"]["true_positives"], 0)
        self.assertEqual(result["metrics"]["false_positives"], 2)
        self.assertEqual(result["metrics"]["miss_rate"], 1)

    def test_one_to_one_matching_maximizes_correct_locations(self):
        labels = {"case_id": "overlap", "expected": [
            {"id": "a", "category": "security.sql_injection", "file_path": "api.py", "line_start": 1, "line_end": 3},
            {"id": "b", "category": "security.sql_injection", "file_path": "api.py", "line_start": 3, "line_end": 5},
        ]}
        result = score_report(report([prediction(line=3), prediction("f-2", line=5)]), labels)
        self.assertEqual(result["metrics"]["localized_matches"], 2)

    def test_clean_case_false_alarm_rate_and_empty_denominators(self):
        clean = {"case_id": "clean", "expected": []}
        negative = score_report(report([]), clean)
        false_alarm = score_report(report(), clean)
        self.assertIsNone(negative["metrics"]["precision"])
        self.assertEqual(negative["metrics"]["clean_false_alarm_rate"], 0)
        self.assertEqual(false_alarm["metrics"]["clean_false_alarm_rate"], 1)

    def test_failed_run_has_no_misleading_quality_score(self):
        failed = score_report(report(status="failed"), LABELS)
        self.assertFalse(failed["eligible"])
        self.assertIsNone(failed["metrics"])
        summary = aggregate([failed, score_report(report(), LABELS)])
        self.assertEqual(summary["failed_cases"], 1)
        self.assertEqual(summary["metrics"]["true_positives"], 1)

    def test_human_annotations_bind_to_run_and_exact_result(self):
        state = report([prediction(category="other")])
        annotations = annotation_template(state)
        annotations["findings"][0].update(reviewed=True, expected_id="sql")
        result = score_report(state, LABELS, annotations)
        self.assertEqual(result["metrics"]["true_positives"], 1)
        self.assertEqual(result["assessment_source"], "human_review")
        annotations["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "belong"):
            score_report(state, LABELS, annotations)

    def test_incomplete_human_review_and_reused_result_are_rejected(self):
        state = report()
        annotations = annotation_template(state)
        with self.assertRaisesRegex(ValueError, "every confirmed"):
            score_report(state, LABELS, annotations)
        annotations["findings"][0].update(reviewed=True, expected_id="sql")
        changed = deepcopy(state)
        changed["findings"][0]["line"] = 90
        with self.assertRaisesRegex(ValueError, "belong"):
            score_report(changed, LABELS, annotations)

    def test_model_regression_pass_does_not_imply_verified_repair(self):
        state = report(status="proposed")
        state["patch"] = {"status": "generated", "diff": "candidate"}
        state["regression"] = {"status": "passed"}
        result = score_report(state, LABELS)
        self.assertIsNone(result["metrics"]["repair_success_rate"])
        self.assertEqual(result["metrics"]["repair_verification_coverage"], 0)

    def test_verified_repair_uses_patch_bound_human_or_isolated_test_evidence(self):
        state = report(status="proposed")
        state["patch"] = {"status": "generated", "diff": "candidate"}
        annotations = annotation_template(state)
        annotations["findings"][0].update(reviewed=True, expected_id="sql")
        annotations["repairs"] = [{"expected_id": "sql", "status": "passed", "method": "isolated_tests",
            "patch_sha256": hashlib.sha256(b"candidate").hexdigest(), "evidence": "isolated-tests.log: security regression passed"}]
        result = score_report(state, LABELS, annotations)
        self.assertEqual(result["metrics"]["repair_success_rate"], 1)
        self.assertEqual(result["metrics"]["repair_verification_coverage"], 1)
        annotations["repairs"][0]["patch_sha256"] = "0"*64
        with self.assertRaisesRegex(ValueError, "candidate patch"):
            score_report(state, LABELS, annotations)

    def test_labels_can_pin_the_reviewed_source_version(self):
        labels = {**LABELS, "source_hashes": {"api.py": "expected-hash"}}
        with self.assertRaisesRegex(ValueError, "source version"):
            score_report(report(), labels)
        state = report()
        state["source_hashes"] = {"api.py": "expected-hash"}
        self.assertEqual(score_report(state, labels)["metrics"]["recall"], 1)

    def test_micro_average_uses_counts_rather_than_averaging_case_percentages(self):
        good = score_report(report(), LABELS)
        bad = score_report(report([prediction("wrong", category="other")]), LABELS)
        combined = aggregate([good, bad])["metrics"]
        self.assertEqual(combined["precision"], 0.5)
        self.assertEqual(combined["recall"], 0.5)


class EvaluationCliTests(unittest.TestCase):
    def test_live_batch_passes_project_sources_and_review_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [{"name": scope, "review_scope": scope, "source_root": root,
                      "file_paths": ["api.py"], "diff": "synthetic input",
                      "sources": {"api.py": "value = 1\n"}, "labels": LABELS}
                     for scope in ("file", "diff")]
            with chdir(root), patch("sys.argv", ["eval"]), patch("evals.cases.load_cases", return_value=cases), patch(
                "src.workflow.service.run_workflow", return_value=report()
            ) as workflow, redirect_stdout(StringIO()):
                run()
            self.assertEqual(workflow.call_count, 2)
            for call, case in zip(workflow.call_args_list, cases):
                self.assertEqual(call.kwargs["review_scope"], case["review_scope"])
                self.assertEqual(call.kwargs["source_root"], root)
                self.assertEqual(call.kwargs["file_contents"], case["sources"])

    def test_saved_result_and_human_template_require_no_model_calls(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"result.json").write_text(json.dumps(report()))
            (root/"labels.json").write_text(json.dumps(LABELS))
            with chdir(root), patch("sys.argv", ["eval", "--report", "result.json", "--labels", "labels.json"]), patch("src.llm.get_fast_llm", side_effect=AssertionError("No model calls")), redirect_stdout(StringIO()):
                run()
            score = json.loads((root/"output/evaluation.json").read_text())
            self.assertEqual(score["summary"]["metrics"]["recall"], 1)
            with chdir(root), patch("sys.argv", ["eval", "--report", "result.json", "--write-annotation-template", "human.json"]), redirect_stdout(StringIO()):
                run()
            annotations = json.loads((root/"human.json").read_text())
            self.assertEqual(annotations["run_id"], "test-run")
            self.assertFalse(annotations["findings"][0]["reviewed"])

    def test_false_negative_and_failed_run_return_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"result.json").write_text(json.dumps(report([])))
            (root/"labels.json").write_text(json.dumps(LABELS))
            with chdir(root), patch("sys.argv", ["eval", "--report", "result.json", "--labels", "labels.json"]), redirect_stdout(StringIO()), self.assertRaises(SystemExit) as error:
                run()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(json.loads((root/"output/evaluation.json").read_text())["summary"]["metrics"]["false_negatives"], 1)


class ProjectCaseTests(unittest.TestCase):
    def test_file_and_diff_cases_validate_versions_without_reading_model_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root/"examples/order_project/app"
            app.mkdir(parents=True)
            (root/"evals/labels").mkdir(parents=True)
            target = app/"stats.py"
            target.write_text(CURRENT)
            (root/"examples/order_project.diff").write_text(unified(BEFORE, CURRENT, "app/stats.py"))
            labels = {"case_id": "fixture", "expected": [],
                      "source_hashes": {"app/stats.py": hashlib.sha256(CURRENT.encode()).hexdigest()}}
            for name in ("order_project", "order_project_diff"):
                (root/"evals/labels"/(name+".json")).write_text(json.dumps(labels))
            with patch("evals.cases.__file__", str(root/"evals/cases.py")):
                cases = load_cases()
                self.assertEqual([c["review_scope"] for c in cases], ["file", "diff"])
                self.assertEqual(cases[0]["sources"], {"app/stats.py": CURRENT})
                target.write_text("changed = True\n")
                with self.assertRaisesRegex(ValueError, "restore"):
                    load_cases()
