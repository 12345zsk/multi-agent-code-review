"""Static search limitations must not become confirmed absence of tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from examples.repair_demo import CURRENT, DemoModel, SAMPLE_DIFF
from src.project_tools import ProjectContext
from src.serialization import to_json
from src.workflow.service import run_workflow


def coverage_finding():
    return {"category": "tests.missing_coverage", "title": "No tests cover average",
            "description": "No tests exist anywhere in the project.", "file_path": "stats.py",
            "line": 1, "severity": "low", "fixable": True,
            "suggestion": "Verify the contract with an empty-list regression case.",
            "evidence": "def average(values):"}


class CoverageModel(DemoModel):
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        self.calls.append((role, payload))
        if role == "review_planner":
            return AIMessage(content=json.dumps({"assignments": [{
                "agent_id": "test_coverage", "reason": "Check regression evidence for the visible contract.",
                "questions": ["Is there evidence of an empty-list regression case?"],
            }]}))
        if role == "test_coverage":
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="", tool_calls=[
                    {"name": "search_code", "args": {"query": "SYNTHETIC_PRIVATE_QUERY_MARKER"},
                     "id": "search-project", "type": "tool_call"},
                    {"name": "search_code", "args": {"query": "average", "directory": "tests"},
                     "id": "search-tests", "type": "tool_call"},
                ])
            return AIMessage(content=json.dumps({"summary": "No tests.", "findings": [coverage_finding()]}))
        if role == "dispute_reviewer":
            return AIMessage(content=json.dumps({"resolutions": [{
                "finding_id": f["id"], "status": "confirmed", "reason": "No search matches prove no tests.",
                "evidence": f["evidence"],
            } for f in payload["candidates"]]}))
        return super().invoke(messages)


class ReviewLimitTests(unittest.TestCase):
    def test_search_limits_reach_adjudication_and_disputes_cannot_override_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"tests").mkdir()
            for i in range(21):
                (root/f"tests/test_{i:02}.py").write_text("value = 1\n")
            original_read = ProjectContext.read
            def unavailable(context, path):
                if path == "tests/test_00.py":
                    raise ValueError("SOURCE_VERSION_CHANGED")
                return original_read(context, path)

            model = CoverageModel()
            with patch.object(ProjectContext, "read", unavailable):
                result = run_workflow(SAMPLE_DIFF, model=model, source_root=root,
                                      file_contents={"stats.py": CURRENT}, mode="fix", run_root=root/"output/runs")
            self.assertEqual(result["status"], "completed", result["errors"])
            self.assertEqual(result["review_round"], 1)
            self.assertEqual(result["fix_attempts"], 0)
            self.assertNotIn("patch", result)
            finding = result["findings"][0]
            self.assertEqual(finding.status, "candidate")
            self.assertFalse(finding.fixable)
            self.assertEqual(result["dispute_reviews"][0]["status"], "candidate")
            checks = list(result["reports"]["test_coverage"].tool_observations)
            self.assertEqual(len(checks), 2)
            self.assertTrue(checks[0]["truncated"])
            self.assertIn("tests/test_00.py", checks[1]["unavailable"])
            for role, payload in model.calls:
                if role in {"adjudicator", "dispute_reviewer"}:
                    self.assertEqual(payload["tool_observations"]["test_coverage"], checks)
            self.assertNotIn("No tests exist", result["final_review"])
            self.assertNotIn("No tests cover", result["final_review"])
            self.assertNotIn("SYNTHETIC_PRIVATE_QUERY_MARKER", to_json(result))
            self.assertIn("NEEDS DISCUSSION", result["final_review"])
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_unknown_or_recategorized_coverage_cannot_bypass_candidate_policy(self):
        class Recategorized(DemoModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                data = json.loads(super().invoke(messages).content)
                if role == "bug_detector":
                    data = {"summary": "No tests.", "findings": [coverage_finding()]}
                if role == "adjudicator":
                    for decision in data["decisions"]:
                        decision["category"] = "other"
                return AIMessage(content=json.dumps(data))

        result = run_workflow(SAMPLE_DIFF, mode="fix", model=Recategorized(), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "completed", result["errors"])
        self.assertEqual(result["findings"][0].category, "tests.missing_coverage")
        self.assertEqual(result["findings"][0].status, "candidate")
        self.assertFalse(result["findings"][0].fixable)
        self.assertEqual(result["fix_attempts"], 0)

    def test_uncertain_coverage_does_not_block_confirmed_functional_repair(self):
        class Mixed(DemoModel):
            def __init__(self):
                self.repair_ids = []
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                if role == "test_coverage":
                    return AIMessage(content=json.dumps({"summary": "No tests.", "findings": [coverage_finding()]}))
                if role == "fix_planner":
                    self.repair_ids = [f["id"] for f in payload["findings"]]
                return super().invoke(messages)

        model = Mixed()
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=model, file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "proposed", result["errors"])
        functional = next(f for f in result["findings"] if f.category == "correctness.empty_input")
        coverage = next(f for f in result["findings"] if f.category == "tests.missing_coverage")
        self.assertEqual(functional.status, "confirmed")
        self.assertEqual(coverage.status, "candidate")
        self.assertEqual(model.repair_ids, [functional.id])
        self.assertNotIn(coverage.id, result["patch"].finding_ids)


if __name__ == "__main__":
    unittest.main()
