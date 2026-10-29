"""Native tool-call loop, bounded reads and durable recovery without network."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from examples.repair_demo import CURRENT, DemoModel, SAMPLE_DIFF
from src.cli import main
from src.model_agent import ModelOutputError
from src.react import MAX_TOOL_CALLS, invoke_react_json, make_read_source
from src.schemas import ReviewReply
from src.workflow.registry import AGENT_IDS
from src.workflow.service import resume_workflow, run_workflow


def request(args=None, *, name="read_source", call_id="read-1"):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args if args is not None else {"file_path": "stats.py"},
        "id": call_id, "type": "tool_call",
    }])


class ReadModel(DemoModel):
    def bind_tools(self, tools, **kwargs):
        assert {tool.name for tool in tools} == {"read_source", "list_project_files", "search_code", "analyze_python"}
        return self

    def invoke(self, messages):
        if messages[0].content.startswith("ROLE: bug_detector\n"):
            if not any(isinstance(message, ToolMessage) for message in messages):
                return request()
        return super().invoke(messages)


class ReadSourceTests(unittest.TestCase):
    def test_reads_numbered_snapshot_lines_without_live_file_access(self):
        sources = {"stats.py": CURRENT}
        read = make_read_source(sources)
        sources["stats.py"] = "changed after snapshot\n"
        result = read.invoke({"file_path": "stats.py", "start_line": 2, "end_line": 3})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["lines"][1], {"line": 3, "text": "    return sum(values) / len(values)"})
        self.assertEqual(len(result["source_hash"]), 64)

    def test_unavailable_and_sensitive_paths_are_not_read(self):
        paths = (".env", "private.key", ".ssh/id_rsa", ".aws/credentials", ".npmrc", "private.pfx")
        read = make_read_source({path: "placeholder" for path in paths})
        for path in ("missing.py", *paths):
            with self.subTest(path=path):
                self.assertEqual(read.invoke({"file_path": path})["code"], "SOURCE_NOT_AVAILABLE")
        for path in ("../outside.py", "/etc/passwd", ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read.invoke({"file_path": path})

    def test_argument_and_output_limits(self):
        read = make_read_source({"stats.py": CURRENT, "large.py": "x"*16001})
        for args in ({"start_line": 0}, {"start_line": True}, {"end_line": 201},
                     {"start_line": 3, "end_line": 2}, {"unknown": 1}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                read.invoke({"file_path": "stats.py", **args})
        self.assertEqual(read.invoke({"file_path": "large.py"})["code"], "TOOL_OUTPUT_TOO_LARGE")
        self.assertEqual(read.invoke({"file_path": "stats.py", "start_line": 10, "end_line": 11})["code"], "LINE_RANGE_INVALID")


class ReactTests(unittest.TestCase):
    def run_review(self, model, **kwargs):
        return run_workflow(SAMPLE_DIFF, model=model,
                            file_contents={"stats.py": CURRENT}, **kwargs)

    def test_tool_result_returns_to_model_and_final_findings_are_validated(self):
        class Observing(ReadModel):
            def invoke(self, messages):
                payload = json.loads(messages[1].content)
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    assert "sources" not in payload
                    assert payload["available_sources"]["stats.py"]["total_lines"] == 3
                    observations = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
                    if observations:
                        assert observations[0]["lines"][2]["line"] == 3
                return super().invoke(messages)

        result = self.run_review(Observing())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"][0].line, 3)
        events = [event for event in result["trace_events"] if event["event"] == "tool_completed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["node"], "bug_detector")
        self.assertEqual(events[0]["tool"], "read_source")
        self.assertEqual(events[0]["status"], "ok")
        self.assertIn("Review strategy: ReAct", result["final_review"])

    def test_tool_argument_error_can_be_corrected_from_observation(self):
        class Correcting(ReadModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    observations = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
                    if not observations:
                        return request({"file_path": "../outside.py"})
                    if len(observations) == 1:
                        assert observations[0]["code"] == "TOOL_ARGUMENTS_INVALID"
                        return request(call_id="read-2")
                return super().invoke(messages)

        result = self.run_review(Correcting())
        self.assertEqual(result["status"], "completed")
        events = [event for event in result["trace_events"] if event["event"] == "tool_completed"]
        self.assertEqual([event["status"] for event in events], ["error", "ok"])

    def test_legal_batch_returns_all_observations_in_order_before_next_model_request(self):
        class Batch(ReadModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: review_planner\n"):
                    observations = [m for m in messages if isinstance(m, ToolMessage)]
                    if not observations:
                        return AIMessage(content="", tool_calls=[request().tool_calls[0],
                            request(name="analyze_python", call_id="ast-2").tool_calls[0]])
                    assert [m.tool_call_id for m in observations] == ["read-1", "ast-2"]
                    assert all(json.loads(m.content)["status"] == "ok" for m in observations)
                return DemoModel.invoke(self, messages)

        result = self.run_review(Batch())
        self.assertEqual(result["status"], "completed", result["errors"])
        events = [e for e in result["trace_events"] if e["event"] == "tool_completed"]
        self.assertEqual([e["tool_call_id"] for e in events], ["read-1", "ast-2"])
        step = next(e for e in result["trace_events"] if e["event"] == "react_step")
        self.assertEqual(step["tool_names"], ["read_source", "analyze_python"])
        self.assertEqual(step["tool_call_count"], 2)

    def test_unknown_tools_and_reused_ids_are_rejected_before_batch_execution(self):
        mixed = AIMessage(content="", tool_calls=[request().tool_calls[0],
            request(name="write_file", call_id="write-2").tool_calls[0]])
        for response in (request(name="write_file"), mixed):
            class Forbidden(ReadModel):
                def invoke(self, messages):
                    return response
            with self.subTest(response=response.tool_calls), self.assertRaises(ModelOutputError) as error:
                with patch("src.project_tools.ProjectContext.read") as read:
                    try:
                        invoke_react_json(Forbidden(), "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
                    finally:
                        read.assert_not_called()
            self.assertEqual(error.exception.code, "TOOL_NOT_ALLOWED")

        duplicate = AIMessage(content="", tool_calls=[request().tool_calls[0], request().tool_calls[0]])
        class Duplicate(ReadModel):
            def invoke(self, messages):
                return duplicate
        with self.assertRaises(ModelOutputError) as error:
            invoke_react_json(Duplicate(), "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
        self.assertEqual(error.exception.code, "TOOL_CALL_ID_INVALID")

        class Reused(ReadModel):
            def invoke(self, messages):
                return request()
        with self.assertRaises(ModelOutputError) as error:
            invoke_react_json(Reused(), "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
        self.assertEqual(error.exception.code, "TOOL_CALL_ID_INVALID")

    def test_batch_reserves_remaining_budget_before_executing_any_of_its_calls(self):
        class BatchLoop(ReadModel):
            def invoke(self, messages):
                count = sum(isinstance(m, ToolMessage) for m in messages)
                return AIMessage(content="", tool_calls=[request(call_id=f"read-{count+i}").tool_calls[0]
                                                        for i in range(3)])
        result = self.run_review(BatchLoop())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(d["error_code"] == "REACT_FINAL_ANSWER_REQUIRED" for d in result["diagnostics"]))
        self.assertEqual(sum(e["event"] == "tool_completed" for e in result["trace_events"]), 6)

    def test_resume_in_middle_of_batch_does_not_repeat_completed_tool(self):
        class Batch(ReadModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: review_planner\n"):
                    observations = [m for m in messages if isinstance(m, ToolMessage)]
                    if not observations:
                        return AIMessage(content="", tool_calls=[request(call_id=f"read-{i}").tool_calls[0]
                                                                for i in range(2)])
                    assert [m.tool_call_id for m in observations] == ["read-0", "read-1"]
                return DemoModel.invoke(self, messages)

        from src.project_tools import ProjectContext
        original_read = ProjectContext.read
        reads = []
        def interrupt_second(context, path):
            reads.append(path)
            if len(reads) == 2:
                raise RuntimeError("SYNTHETIC_BATCH_INTERRUPTION")
            return original_read(context, path)

        with tempfile.TemporaryDirectory() as root:
            with patch.object(ProjectContext, "read", interrupt_second):
                failed = self.run_review(Batch(), run_root=root)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(sum(e["event"] == "tool_completed" for e in failed["trace_events"]), 1)
            resumed = resume_workflow(failed["run_id"], run_root=root, model=Batch())
            self.assertEqual(resumed["status"], "completed", resumed["errors"])
            self.assertEqual([e["tool_call_id"] for e in resumed["trace_events"] if e["event"] == "tool_completed"],
                             ["read-0", "read-1"])

    def test_tool_loop_is_bounded_and_cannot_report_success(self):
        class Loop(ReadModel):
            def invoke(self, messages):
                return request(call_id=f"read-{sum(isinstance(message, ToolMessage) for message in messages)}")
        result = self.run_review(Loop())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item["error_code"] == "REACT_FINAL_ANSWER_REQUIRED" for item in result["diagnostics"]))
        self.assertEqual(sum(event["event"] == "tool_completed" for event in result["trace_events"]), MAX_TOOL_CALLS)

    def test_budget_exhaustion_uses_model_without_tools_and_reaches_adjudication(self):
        class BudgetReader(ReadModel):
            def bind_tools(self, tools, **kwargs):
                parent = self
                class Bound:
                    def invoke(self, messages):
                        if messages[0].content.startswith("ROLE: bug_detector\n"):
                            count = sum(isinstance(m, ToolMessage) for m in messages)
                            assert f"remaining_tool_calls={MAX_TOOL_CALLS-count}" in messages[0].content
                            assert count < MAX_TOOL_CALLS, "Exhausted calls must use the unbound model"
                            return AIMessage(content="", tool_calls=[
                                request(call_id="read-0").tool_calls[0] if count+i == 0 else
                                request({"query": "average"}, name="search_code", call_id=f"search-{count+i}").tool_calls[0]
                                for i in range(2)])
                        return DemoModel.invoke(parent, messages)
                return Bound()

            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    assert "remaining_tool_calls=0" in messages[0].content
                    assert "Do not request tools" in messages[0].content
                    assert sum(isinstance(m, ToolMessage) for m in messages) == MAX_TOOL_CALLS
                return DemoModel.invoke(self, messages)

        result = self.run_review(BudgetReader())
        self.assertEqual(result["status"], "completed", result["errors"])
        self.assertEqual(result["findings"][0].status, "confirmed")
        self.assertEqual(sum(e["event"] == "tool_completed" for e in result["trace_events"]), MAX_TOOL_CALLS)
        self.assertEqual(sum(e["event"] == "tool_completed" and e["tool"] == "search_code"
                             for e in result["trace_events"]), 7)
        self.assertTrue(any(e["event"] == "react_finalization" for e in result["trace_events"]))
        self.assertIn("Context collection limited", result["final_review"])

    def test_final_json_retry_after_eight_single_calls_respects_model_request_limit(self):
        class Finishing(ReadModel):
            def __init__(self):
                self.requests = 0
            def invoke(self, messages):
                self.requests += 1
                assert f"remaining_model_requests={11-self.requests}" in messages[0].content
                count = sum(isinstance(m, ToolMessage) for m in messages)
                if count < MAX_TOOL_CALLS:
                    return request(call_id=f"read-{count}")
                if isinstance(messages[-1], ToolMessage):
                    return AIMessage(content="SYNTHETIC_INVALID_FINAL_JSON")
                return AIMessage(content='{"summary":"Limited to supplied evidence.","findings":[]}')

        model = Finishing()
        result = invoke_react_json(model, "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
        self.assertEqual(model.requests, 10)
        self.assertEqual(result.findings, [])

    def test_oversized_batch_gets_error_observations_and_bounded_final_json_retry(self):
        class Oversized(ReadModel):
            def __init__(self):
                self.final_attempts = 0
            def bind_tools(self, tools, **kwargs):
                class Bound:
                    def invoke(self, messages):
                        assert "remaining_tool_calls=8" in messages[0].content
                        return AIMessage(content="", tool_calls=[request(call_id=f"read-{i}").tool_calls[0]
                                                                for i in range(MAX_TOOL_CALLS+1)])
                return Bound()
            def invoke(self, messages):
                observations = [m for m in messages if isinstance(m, ToolMessage)]
                assert [m.tool_call_id for m in observations] == [f"read-{i}" for i in range(MAX_TOOL_CALLS+1)]
                assert all(json.loads(m.content)["code"] == "REACT_TOOL_LIMIT_EXCEEDED" for m in observations)
                assert "Do not request tools" in messages[0].content
                self.final_attempts += 1
                if self.final_attempts == 1:
                    return AIMessage(content="SYNTHETIC_INVALID_FINAL_RESPONSE")
                assert "SYNTHETIC_INVALID_FINAL_RESPONSE" not in str(messages)
                return AIMessage(content='{"summary":"Evidence is limited.","findings":[]}')

        model = Oversized()
        with patch("src.project_tools.ProjectContext.read") as read:
            result = invoke_react_json(model, "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
            read.assert_not_called()
        self.assertEqual(result.findings, [])
        self.assertEqual(model.final_attempts, 2)

    def test_limited_context_with_no_findings_does_not_report_approval(self):
        class NoEvidence(ReadModel):
            def bind_tools(self, tools, **kwargs):
                parent = self
                class Bound:
                    def invoke(self, messages):
                        if messages[0].content.startswith("ROLE: bug_detector\n"):
                            return AIMessage(content="", tool_calls=[request(call_id=f"read-{i}").tool_calls[0]
                                                                    for i in range(MAX_TOOL_CALLS+1)])
                        data = DemoModel.invoke(parent, messages)
                        if messages[0].content.splitlines()[0].removeprefix("ROLE: ") in AGENT_IDS:
                            return AIMessage(content='{"summary":"No evidence.","findings":[]}')
                        return data
                return Bound()
            def invoke(self, messages):
                return AIMessage(content='{"summary":"Insufficient evidence.","findings":[]}')

        result = self.run_review(NoEvidence())
        self.assertEqual(result["status"], "completed", result["errors"])
        self.assertIn("NEEDS DISCUSSION", result["final_review"])
        self.assertIn("Context collection limited", result["final_review"])
        self.assertNotIn("APPROVE", result["final_review"])

    def test_resume_at_budget_finalization_keeps_observations_without_repeating_tools(self):
        class Interrupted(ReadModel):
            def __init__(self, interrupt):
                self.interrupt = interrupt
            def bind_tools(self, tools, **kwargs):
                parent = self
                class Bound:
                    def invoke(self, messages):
                        if messages[0].content.startswith("ROLE: bug_detector\n"):
                            return AIMessage(content="", tool_calls=[request(call_id=f"read-{i}").tool_calls[0]
                                                                    for i in range(MAX_TOOL_CALLS)])
                        return DemoModel.invoke(parent, messages)
                return Bound()
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    assert sum(isinstance(m, ToolMessage) for m in messages) == MAX_TOOL_CALLS
                    if self.interrupt:
                        raise ConnectionError("SYNTHETIC_FINALIZATION_INTERRUPTION")
                return DemoModel.invoke(self, messages)

        with tempfile.TemporaryDirectory() as root:
            failed = self.run_review(Interrupted(True), run_root=root)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(sum(e["event"] == "tool_completed" for e in failed["trace_events"]), MAX_TOOL_CALLS)
            resumed = resume_workflow(failed["run_id"], run_root=root, model=Interrupted(False))
            self.assertEqual(resumed["status"], "completed", resumed["errors"])
            self.assertEqual(sum(e["event"] == "tool_completed" for e in resumed["trace_events"]), MAX_TOOL_CALLS)

    def test_final_json_retry_preserves_observation_without_rereading(self):
        class Formatting(ReadModel):
            def invoke(self, messages):
                if (messages[0].content.startswith("ROLE: bug_detector\n")
                        and isinstance(messages[-1], ToolMessage)):
                    return AIMessage(content="invalid-final-response-marker")
                assert "invalid-final-response-marker" not in str(messages)
                return super().invoke(messages)
        result = self.run_review(Formatting())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(event["event"] == "tool_completed" for event in result["trace_events"]), 1)

    def test_resume_after_tool_does_not_repeat_completed_read(self):
        class Interrupted(ReadModel):
            def invoke(self, messages):
                if isinstance(messages[-1], ToolMessage):
                    raise ConnectionError("SENSITIVE_TEST_MARKER")
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as root:
            failed = self.run_review(Interrupted(), run_root=root)
            self.assertEqual(failed["status"], "failed")
            for path in Path(failed["run_dir"]).rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"SENSITIVE_TEST_MARKER", path.read_bytes())
            resumed = resume_workflow(failed["run_id"], run_root=root, model=ReadModel())
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(sum(event["event"] == "tool_completed" for event in resumed["trace_events"]), 1)
            self.assertEqual(len(resumed["reports"]["bug_detector"].tool_observations), 1)

    def test_react_fix_retains_existing_validation_pipeline(self):
        result = self.run_review(ReadModel(), mode="fix")
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["validation"].status, "passed")
        self.assertEqual(result["runtime_validation"].status, "not_run")

    def test_legacy_run_resumes_without_switching_checkpoint_strategy(self):
        from src.runtime.store import RunStore

        class LegacyModel(DemoModel):
            def __init__(self, interrupt=False):
                self.interrupt = interrupt

            def bind_tools(self, tools, **kwargs):
                raise AssertionError("A legacy direct-mode run must not bind tools")

            def invoke(self, messages):
                if self.interrupt and messages[0].content.startswith("ROLE: bug_detector\n"):
                    raise ConnectionError("Simulated legacy interruption")
                return super().invoke(messages)

        create = RunStore.create

        def create_legacy_run(run_root, inputs):
            # Reproduce inputs persisted before ReAct became the default.
            inputs["react"] = False
            return create(run_root, inputs)

        with tempfile.TemporaryDirectory() as root:
            with patch("src.workflow.service.RunStore.create", side_effect=create_legacy_run):
                failed = self.run_review(LegacyModel(interrupt=True), run_root=root)
            self.assertEqual(failed["status"], "failed")
            resumed = resume_workflow(failed["run_id"], run_root=root, model=LegacyModel())
            self.assertEqual(resumed["status"], "completed")
            self.assertFalse(resumed["react"])
            self.assertFalse(any(event["event"] == "tool_completed" for event in resumed["trace_events"]))

    def test_parallel_agents_have_independent_tool_histories_and_checkpoints(self):
        class AllReaders(ReadModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                if role in AGENT_IDS and not any(isinstance(message, ToolMessage) for message in messages):
                    return request()
                return super().invoke(messages)

        with tempfile.TemporaryDirectory() as root:
            result = run_workflow(SAMPLE_DIFF, model=AllReaders(),
                                  file_contents={"stats.py": CURRENT}, run_root=root)
            self.assertEqual(result["status"], "completed")
            events = [event for event in result["trace_events"] if event["event"] == "tool_completed"]
            self.assertEqual(len(events), len(AGENT_IDS))
            self.assertEqual({event["node"] for event in events}, set(AGENT_IDS))

    def test_model_without_tool_binding_fails_explicitly(self):
        class NoTools:
            pass
        result = self.run_review(NoTools())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostics"][0]["error_code"], "MODEL_TOOL_BINDING_FAILED")

    def test_cli_uses_and_persists_react_without_a_flag(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"stats.py").write_text(CURRENT)
            with chdir(root), patch("sys.argv", ["code-review", "--file", "input.diff", "--source-root", "."]), patch(
                "src.llm.get_fast_llm", return_value=ReadModel()
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main()
            latest = json.loads((root/"output/latest.json").read_text())
            report = json.loads((Path(latest["run_dir"])/"result.json").read_text())
            self.assertTrue(report["react"])
            self.assertEqual(report["status"], "completed")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_scaffold_stays_offline_without_claiming_react(self):
        from unittest.mock import Mock
        model = Mock()
        result = run_workflow(SAMPLE_DIFF, model=model, scaffold=True)
        self.assertFalse(result["react"])
        self.assertEqual(result["status"], "incomplete")
        model.bind_tools.assert_not_called()
        model.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
