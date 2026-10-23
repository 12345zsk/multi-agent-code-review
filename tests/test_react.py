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
from src.react import invoke_react_json, make_read_source
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
        assert [tool.name for tool in tools] == ["read_source"]
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
        read = make_read_source({".env": "placeholder", "private.key": "placeholder"})
        for path in ("missing.py", ".env", "private.key"):
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
                            active_agents=["bug_detector"], file_contents={"stats.py": CURRENT}, **kwargs)

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

    def test_unknown_tools_parallel_calls_and_reused_ids_are_rejected(self):
        parallel = request()
        parallel.tool_calls.append({"name": "read_source", "args": {"file_path": "stats.py"}, "id": "read-2", "type": "tool_call"})
        for response in (request(name="write_file"), parallel):
            class Forbidden(ReadModel):
                def invoke(self, messages):
                    return response
            with self.subTest(response=response.tool_calls), self.assertRaises(ModelOutputError) as error:
                invoke_react_json(Forbidden(), "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
            self.assertEqual(error.exception.code, "TOOL_NOT_ALLOWED")

        class Reused(ReadModel):
            def invoke(self, messages):
                return request()
        with self.assertRaises(ModelOutputError) as error:
            invoke_react_json(Reused(), "test", "Review", {}, ReviewReply, sources={"stats.py": CURRENT})
        self.assertEqual(error.exception.code, "TOOL_CALL_ID_INVALID")

    def test_tool_loop_is_bounded_and_cannot_report_success(self):
        class Loop(ReadModel):
            def invoke(self, messages):
                return request(call_id=f"read-{sum(isinstance(message, ToolMessage) for message in messages)}")
        result = self.run_review(Loop())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item["error_code"] == "REACT_TOOL_LIMIT_EXCEEDED" for item in result["diagnostics"]))
        self.assertEqual(sum(event["event"] == "tool_completed" for event in result["trace_events"]), 3)

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
