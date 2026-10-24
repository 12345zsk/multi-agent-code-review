"""Offline contract tests for orchestration, not model review accuracy."""

import unittest
from unittest.mock import Mock

from src.contracts import (
    AgentReport, Finding, FixPlan, PatchArtifact, ValidationResult,
)
from src.workflow.registry import AGENT_IDS
from src.workflow.service import run_workflow as _run_workflow


def run_workflow(*args, **kwargs):
    return _run_workflow(*args, scaffold=True, **kwargs)

DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return a\n+return a / b\n"
ISSUE = Finding(
    "bug-1", "bug_detector", "Missing zero check", "Check the denominator.",
    "app.py", line=1, status="confirmed", fixable=True,
)


def completed_review_handlers():
    return {
        "review_planner": lambda state: {"active_agents": list(AGENT_IDS)},
        **{
            name: (lambda state, agent=name: {
                "reports": {agent: AgentReport(agent, status="completed")}
            })
            for name in AGENT_IDS
        },
        "adjudicator": lambda state: {"findings": [ISSUE]},
    }


def completed_fix_handlers():
    return {
        **completed_review_handlers(),
        "fix_planner": lambda state: {"fix_plan": FixPlan("ready", (ISSUE.id,))},
        "patch_generator": lambda state: {
            "patch": PatchArtifact("generated", "synthetic patch", (ISSUE.id,))
        },
        "test_generator": lambda state: {"test_patch": PatchArtifact("generated")},
        "validator": lambda state: {"validation": ValidationResult("passed", ("fake-check",))},
        "regression_reviewer": lambda state: {"regression": ValidationResult("passed")},
        "failure_diagnoser": lambda state: {"failure_diagnosis": {"action": "retry"}},
    }


class WorkflowTests(unittest.TestCase):
    def test_scaffold_is_explicitly_incomplete_and_does_not_fix(self):
        result = run_workflow(DIFF, mode="fix")
        self.assertEqual(result["reports"], {})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["fix_attempts"], 0)
        self.assertEqual(result["pending_steps"], ["review_planner"])
        self.assertNotIn("patch", result)

    def test_all_parallel_reports_join_once_before_adjudication(self):
        calls = []
        handlers = completed_review_handlers()

        def adjudicate(state):
            calls.append(set(state["reports"]))
            return {"findings": []}

        handlers["adjudicator"] = adjudicate
        result = run_workflow(DIFF, handlers=handlers)
        self.assertEqual(calls, [set(AGENT_IDS)])
        self.assertEqual(result["status"], "completed")

    def test_planner_output_controls_dispatch_without_a_selection_argument(self):
        for selected in (["security"], []):
            with self.subTest(selected=selected):
                handlers = completed_review_handlers()
                handlers["review_planner"] = lambda state: {"active_agents": selected}
                result = run_workflow(DIFF, handlers=handlers)
                self.assertEqual(set(result["reports"]), set(selected))
                self.assertIn("final_review", result)

    def test_review_mode_never_calls_fix_nodes(self):
        handlers = completed_review_handlers()
        planner = Mock(side_effect=AssertionError("Review cannot fix"))
        handlers["fix_planner"] = planner
        result = run_workflow(DIFF, mode="review", handlers=handlers)
        planner.assert_not_called()
        self.assertEqual(result["fix_attempts"], 0)

    def test_successful_fix_requires_validation_and_regression(self):
        result = run_workflow(DIFF, mode="fix", handlers=completed_fix_handlers())
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["fix_attempts"], 1)
        self.assertEqual(result["validation"].status, "passed")
        self.assertEqual(result["regression"].status, "passed")

    def test_fix_stubs_cannot_report_success(self):
        result = run_workflow(DIFF, mode="fix", handlers=completed_review_handlers())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["fix_attempts"], 1)
        self.assertEqual(result["fix_plan"].status, "pending")

    def test_failed_validation_retries_to_limit(self):
        handlers = completed_fix_handlers()
        handlers["validator"] = lambda state: {"validation": ValidationResult("failed")}
        result = run_workflow(DIFF, mode="fix", max_fix_attempts=3, handlers=handlers)
        self.assertEqual(result["fix_attempts"], 3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["regression"].status, "not_run")

    def test_regression_retry_clears_previous_artifacts(self):
        seen = []
        handlers = completed_fix_handlers()

        def plan(state):
            seen.append((state["patch"], state["validation"].status, state["regression"].status))
            return {"fix_plan": FixPlan("ready", (ISSUE.id,))}

        handlers["fix_planner"] = plan
        handlers["regression_reviewer"] = lambda state: {
            "regression": ValidationResult("failed" if state["fix_attempts"] == 1 else "passed")
        }
        result = run_workflow(DIFF, mode="fix", handlers=handlers)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["fix_attempts"], 2)
        self.assertEqual(seen, [(None, "not_run", "not_run")] * 2)

    def test_missing_validation_is_not_a_pass(self):
        handlers = completed_fix_handlers()
        handlers["validator"] = lambda state: {}
        result = run_workflow(DIFF, mode="fix", handlers=handlers)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["regression"].status, "not_run")

    def test_specialist_cannot_update_shared_scalar_or_other_report(self):
        for update in (
            {"mode": "fix"},
            {"reports": {"security": AgentReport("security")}},
        ):
            handlers = completed_review_handlers()
            handlers["bug_detector"] = lambda state: update
            result = run_workflow(DIFF, mode="fix", handlers=handlers)
            self.assertEqual(result["reports"]["bug_detector"].status, "failed")
            self.assertEqual(result["fix_attempts"], 0)

    def test_exception_diagnostics_do_not_copy_raw_messages(self):
        handlers = completed_review_handlers()

        def broken(state):
            raise RuntimeError("SENSITIVE_TEST_MARKER")

        handlers["security"] = broken
        result = run_workflow(DIFF, mode="fix", handlers=handlers)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SENSITIVE_TEST_MARKER", repr(result))
        self.assertEqual(result["fix_attempts"], 0)

    def test_invalid_configuration_fails_early(self):
        for options in (
            {"active_agents": ["unknown"]}, {"mode": "other"},
            {"max_fix_attempts": 0}, {"max_fix_attempts": True},
            {"handlers": {"unknown": lambda state: {}}},
        ):
            with self.subTest(options=list(options)), self.assertRaises(ValueError):
                run_workflow(DIFF, **options)


if __name__ == "__main__":
    unittest.main()
