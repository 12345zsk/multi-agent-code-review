"""Production nodes exercised with synthetic models; no network or target execution."""

import json
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, unified
from src.contracts import AgentReport, Finding, FixPlan, PatchArtifact
from src.model_agent import ModelOutputError, invoke_json
from src.repair.nodes import validator_node
from src.schemas import ReviewReply
from src.serialization import to_json
from src.workflow.registry import AGENT_IDS
from src.workflow.service import run_workflow


class AlteredModel(DemoModel):
    def __init__(self, transform):
        self.transform = transform
        self.calls = []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        data = json.loads(super().invoke(messages).content)
        self.calls.append((role, payload))
        return AIMessage(content=json.dumps(self.transform(role, payload, data)))


class LiveWorkflowTests(unittest.TestCase):
    def test_all_nodes_generate_statically_valid_proposal_without_mutating_source(self):
        sources = {"stats.py": CURRENT}
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=DemoModel(), file_contents=sources)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(set(result["reports"]), set(AGENT_IDS))
        self.assertEqual(result["candidate_files"]["stats.py"], BEFORE)
        self.assertEqual(result["validation"].status, "passed")
        self.assertEqual(result["validation"].scope, "static")
        self.assertEqual(result["runtime_validation"].status, "not_run")
        self.assertEqual(sources, {"stats.py": CURRENT})
        self.assertIn("Candidate code patch", result["final_review"])

    def test_review_mode_stops_after_adjudication(self):
        model = AlteredModel(lambda role, payload, data: data)
        result = run_workflow(SAMPLE_DIFF, model=model)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"][0].status, "confirmed")
        self.assertNotIn("patch", result)
        self.assertNotIn("patch_generator", [role for role, _ in model.calls])

    def test_clean_input_has_no_fabricated_findings_or_fix(self):
        model = AlteredModel(lambda role, payload, data: {"summary": "No supported issues.", "findings": []} if role in AGENT_IDS else data)
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=model)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["fix_attempts"], 0)
        self.assertIn("APPROVE", result["final_review"])

    def test_missing_source_limits_validation_but_still_returns_proposal(self):
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=DemoModel())
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["validation"].status, "not_run")
        self.assertIn("complete post-change source is missing", result["validation"].summary)

    def test_unsupported_evidence_blocks_fix(self):
        def transform(role, payload, data):
            if role == "bug_detector":
                data["findings"][0]["evidence"] = "this code does not exist"
            return data
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fix_attempts"], 0)

    def test_arbitrator_cannot_invent_ids(self):
        def transform(role, payload, data):
            if role == "adjudicator":
                data["decisions"][0]["finding_id"] = "invented"
            return data
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fix_attempts"], 0)

    def test_candidate_or_rejected_issue_does_not_trigger_fix(self):
        for status in ("candidate", "rejected"):
            def transform(role, payload, data):
                if role == "adjudicator":
                    data["decisions"][0]["status"] = status
                return data
            result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform))
            self.assertEqual(result["fix_attempts"], 0)
            self.assertNotIn("patch", result)

    def test_bad_patch_retries_and_receives_previous_feedback(self):
        def transform(role, payload, data):
            if role == "patch_generator" and not payload["previous_attempt"]["patch"]:
                data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
            return data
        model = AlteredModel(transform)
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=model, file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["fix_attempts"], 2)
        plans = [payload for role, payload in model.calls if role == "fix_planner"]
        self.assertIn("SyntaxError", plans[1]["previous_attempt"]["validation"])

    def test_invalid_patch_stops_at_attempt_limit(self):
        def transform(role, payload, data):
            if role == "patch_generator":
                data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
            return data
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fix_attempts"], 2)

    def test_json_export_excludes_raw_inputs_and_includes_artifacts(self):
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=DemoModel(), file_contents={"stats.py": CURRENT})
        data = json.loads(to_json(result))
        self.assertNotIn("code_diff", data)
        self.assertNotIn("file_contents", data)
        self.assertNotIn("candidate_files", data)
        self.assertTrue(data["patch"]["diff"])
        self.assertEqual(data["runtime_validation"]["status"], "not_run")

    def test_duplicate_findings_use_adjudicated_severity(self):
        duplicate = Finding(
            "security-duplicate", "security", "Same empty-input bug", "Same visible contract violation.",
            "stats.py", line=3, severity="critical", fixable=True,
            evidence="return sum(values) / len(values)",
        )
        def transform(role, payload, data):
            if role == "adjudicator":
                canonical = next(item["id"] for item in payload["candidates"] if item["agent_id"] == "bug_detector")
                for decision in data["decisions"]:
                    if decision["finding_id"] == duplicate.id:
                        decision["duplicate_of"] = canonical
            return data
        result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform), handlers={
            "security": lambda state: {"reports": {"security": AgentReport("security", "completed", (duplicate,))}}
        })
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0].severity, "high")

    def test_finding_line_is_resolved_from_evidence(self):
        def transform(role, payload, data):
            if role == "bug_detector":
                data["findings"][0]["line"] = 1
            return data
        result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform))
        self.assertEqual(result["findings"][0].line, 3)

    def test_unverified_specialist_summary_is_not_published(self):
        def transform(role, payload, data):
            if role == "performance":
                data["summary"] = "This is a hot path and the repository has no tests."
            return data
        result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform))
        self.assertNotIn("hot path", result["final_review"])
        self.assertNotIn("hot path", to_json(result))

    def test_adjudicator_rewrites_unsupported_secondary_claim(self):
        corrected = "Empty input violates the documented return contract."
        def transform(role, payload, data):
            if role == "bug_detector":
                data["findings"][0]["title"] += " and unsupported type mismatch"
                data["findings"][0]["description"] += " It is a hot path with no repository tests."
            elif role == "adjudicator":
                data["decisions"][0]["title"] = "Empty input divides by zero"
                data["decisions"][0]["description"] = corrected
                data["decisions"][0]["suggestion"] = "Return zero for an empty list."
            return data
        result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform))
        self.assertEqual(result["findings"][0].description, corrected)
        self.assertEqual(result["findings"][0].suggestion, "Return zero for an empty list.")
        self.assertNotIn("hot path", result["final_review"])
        self.assertNotIn("unsupported type mismatch", result["final_review"])
        self.assertIn("Severity basis:", result["final_review"])

    def test_critical_without_verified_impact_and_exposure_is_capped(self):
        for support in ({}, {"critical_impact_evidence": "return sum(values) / len(values)",
                             "critical_exposure_evidence": "public production endpoint"}):
            def transform(role, payload, data):
                if role == "adjudicator":
                    data["decisions"][0].update(severity="critical", **support)
                return data
            result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["findings"][0].severity, "high")
            self.assertIn("Limited to high", result["findings"][0].severity_reason)

    def test_critical_with_supplied_support_keeps_evidence_in_report(self):
        # This synthetic response tests the evidence gate, not the model's impact judgment.
        def transform(role, payload, data):
            if role == "adjudicator":
                data["decisions"][0].update(
                    severity="critical", critical_impact_evidence="return sum(values) / len(values)",
                    critical_exposure_evidence="def average(values):",
                )
            return data
        result = run_workflow(SAMPLE_DIFF, model=AlteredModel(transform))
        self.assertEqual(result["findings"][0].severity, "critical")
        self.assertIn("Critical exposure evidence: def average(values):", result["final_review"])

    def test_specialists_receive_numbered_target_context(self):
        model = AlteredModel(lambda role, payload, data: data)
        run_workflow(SAMPLE_DIFF, model=model)
        payload = next(payload for role, payload in model.calls if role == "bug_detector")
        self.assertEqual(payload["target_lines"]["stats.py"]["3"], "    return sum(values) / len(values)")

    def test_model_regression_failure_retries_with_feedback(self):
        def transform(role, payload, data):
            if role == "regression_reviewer" and not payload["previous_attempt"]["regression"]:
                data = {"status": "failed", "summary": "Please preserve the visible contract more explicitly."}
            return data
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["fix_attempts"], 2)
        self.assertIn("visible contract", result["previous_attempt"]["regression"])

    def test_uncertain_model_regression_is_incomplete(self):
        def transform(role, payload, data):
            return {"status": "not_run", "summary": "Insufficient evidence to judge."} if role == "regression_reviewer" else data
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=AlteredModel(transform))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["fix_attempts"], 1)


class OutputTests(unittest.TestCase):
    def test_non_model_message_is_rejected_by_production_adapter(self):
        model = Mock()
        model.invoke.return_value = HumanMessage(content="wrong response type")
        with self.assertRaises(ModelOutputError) as error:
            invoke_json(model, "test", "Review", {}, ReviewReply)
        self.assertEqual(error.exception.code, "MODEL_RESPONSE_UNSUPPORTED")

    def test_invalid_tool_calls_are_rejected_by_production_adapter(self):
        model = Mock()
        model.invoke.return_value = AIMessage(content="", invalid_tool_calls=[{
            "name": "read_source", "args": "invalid-json", "id": "invalid-call",
            "error": "invalid arguments", "type": "invalid_tool_call",
        }])
        with self.assertRaises(ModelOutputError) as error:
            invoke_json(model, "test", "Review", {}, ReviewReply)
        self.assertEqual(error.exception.code, "MODEL_RESPONSE_UNSUPPORTED")

    def test_invalid_json_retries_once_without_echoing_invalid_content(self):
        model = Mock()
        model.invoke.side_effect = [AIMessage(content="invalid-response-marker"), AIMessage(content='{"summary":"Clean", "findings":[]}')]
        reply = invoke_json(model, "test", "Test", {}, ReviewReply)
        self.assertEqual(reply.findings, [])
        self.assertEqual(model.invoke.call_count, 2)
        self.assertNotIn("invalid-response-marker", str(model.invoke.call_args))

    def test_invalid_schema_exhaustion_and_tool_calls_fail(self):
        for message in (AIMessage(content='{"summary":1,"findings":[]}'), AIMessage(content="", tool_calls=[{"name":"forbidden", "args":{}, "id":"test"}])):
            model = Mock()
            model.invoke.return_value = message
            with self.assertRaises(ModelOutputError):
                invoke_json(model, "test", "Test", {}, ReviewReply)
            self.assertLessEqual(model.invoke.call_count, 2)
            model.bind_tools.assert_not_called()

    def test_fenced_and_text_block_json(self):
        for content in ('```json\n{"summary":"Clean", "findings":[]}\n```', [{"type":"text", "text":'{"summary":"Clean", "findings":[]}'}]):
            model = Mock()
            model.invoke.return_value = AIMessage(content=content)
            self.assertEqual(invoke_json(model, "test", "Test", {}, ReviewReply).summary, "Clean")


class ValidatorTests(unittest.TestCase):
    def state(self, diff, *, test_diff="", sources=None):
        return {
            "patch": PatchArtifact("generated", diff, ("f-1",)),
            "test_patch": PatchArtifact("generated" if test_diff else "not_needed", test_diff, ("f-1",), "Reuse existing tests."),
            "fix_plan": FixPlan("ready", ("f-1",)), "file_paths": ["stats.py"],
            "file_contents": {"stats.py": CURRENT} if sources is None else sources,
        }

    def test_non_test_paths_outside_scope_and_context_mismatch(self):
        for state in (
            self.state(unified(CURRENT, BEFORE, "other.py")),
            self.state(unified(CURRENT, BEFORE, "stats.py"), test_diff=unified("", "x=1\n", "settings.py", new=True)),
            self.state(unified(CURRENT, BEFORE, "stats.py"), sources={"stats.py": "different\n"}),
        ):
            self.assertEqual(validator_node(state)["validation"].status, "failed")

    def test_identical_patch_and_redacted_patch_are_not_valid_fixes(self):
        noop = '--- a/stats.py\n+++ b/stats.py\n@@ -1 +1 @@\n-def average(values):\n+def average(values):\n'
        for patch in (noop, unified(CURRENT, BEFORE + '# <REDACTED>\n', "stats.py")):
            self.assertEqual(validator_node(self.state(patch))["validation"].status, "failed")

    def test_python_check_does_not_execute_candidate_code(self):
        source = BEFORE + '\nraise RuntimeError("must not execute")\n'
        result = validator_node(self.state(unified(CURRENT, source, "stats.py")))
        self.assertEqual(result["validation"].status, "passed")


if __name__ == "__main__":
    unittest.main()
