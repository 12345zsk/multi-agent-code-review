"""End-to-end offline example using a deterministic model, never real API calls."""

import difflib
import json

from langchain_core.messages import AIMessage

from src.workflow.service import run_workflow

BEFORE = '''def average(values):
    """Return zero for an empty list."""
    if not values:
        return 0
    return sum(values) / len(values)
'''
CURRENT = '''def average(values):
    """Return zero for an empty list."""
    return sum(values) / len(values)
'''
TESTS = '''import unittest
from stats import average


class AverageTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(average([]), 0)

    def test_normal(self):
        self.assertEqual(average([2, 4]), 3)
'''


def unified(before, after, path, *, new=False):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="/dev/null" if new else "a/" + path, tofile="b/" + path,
    ))


SAMPLE_DIFF = unified(BEFORE, CURRENT, "stats.py")


class DemoModel:
    """Deterministic synthetic responses to exercise actual production parsers."""

    def bind_tools(self, tools, **kwargs):
        # This synthetic model ends directly; native tool loops have dedicated tests.
        return self

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        if role == "bug_detector":
            data = {"summary": "Empty input now divides by zero.", "findings": [{
                "title": "Empty input divides by zero", "description": "The removed guard violates the documented empty-list contract.",
                "file_path": "stats.py", "line": 3, "severity": "high", "fixable": True,
                "suggestion": "Restore the empty-list guard.", "evidence": "return sum(values) / len(values)",
                "category": "correctness.empty_input",
            }]}
        elif role == "adjudicator":
            data = {"decisions": [{
                "finding_id": item["id"], "status": "confirmed",
                "reason": "The diff removes the guard and preserves the zero-on-empty contract.",
                "title": item["title"], "description": item["description"], "suggestion": item["suggestion"] or "Restore the empty-list guard.",
                "category": item.get("category", "other"),
                "severity": item["severity"], "severity_reason": "Empty input crashes instead of meeting the explicit return contract.",
                "duplicate_of": None,
            } for item in payload["candidates"]]}
        elif role == "fix_planner":
            data = {"finding_ids": [item["id"] for item in payload["findings"]], "description": "Restore the guard without changing normal-list behavior."}
        elif role == "patch_generator":
            data = {"finding_ids": payload["plan"]["finding_ids"], "diff": unified(CURRENT, BEFORE, "stats.py"), "summary": "Restore the empty-list guard."}
        elif role == "test_generator":
            data = {"status": "generated", "diff": unified("", TESTS, "tests/test_stats.py", new=True), "summary": "Propose empty and normal list cases. Tests have not been executed."}
        elif role == "regression_reviewer":
            data = {"status": "passed", "summary": "The proposed guard satisfies the visible contract. This is synthetic model review, not runtime validation."}
        else:
            data = {"summary": "No additional supported issues in this synthetic example.", "findings": []}
        return AIMessage(content=json.dumps(data))


def run_demo():
    return run_workflow(SAMPLE_DIFF, mode="fix", model=DemoModel(), file_contents={"stats.py": CURRENT})


if __name__ == "__main__":
    print("OFFLINE DEMO: synthetic model responses; no API requests or test execution.\n")
    print(run_demo()["final_review"])
