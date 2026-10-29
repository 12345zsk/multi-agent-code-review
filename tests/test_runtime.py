"""Persistent recovery, failure provenance and run-owned artifact regression tests."""

from contextlib import chdir, redirect_stderr, redirect_stdout
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, unified
from src.cli import main
from src.runtime.store import RunStore, file_lock, publish_run, write_json
from src.workflow.registry import AGENT_IDS
from src.workflow.service import resume_workflow, run_workflow


class ChangedModel(DemoModel):
    def __init__(self, change):
        self.change = change

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        data = json.loads(super().invoke(messages).content)
        return AIMessage(content=json.dumps(self.change(role, data)))


def fail_security(role, data):
    if role == "security":
        raise ConnectionError("SENSITIVE_TEST_MARKER")
    return data


def invalid_patch(role, data):
    if role == "patch_generator":
        data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
    return data


class RuntimeTests(unittest.TestCase):
    def execute(self, root, model= None, **kwargs):
        return run_workflow(SAMPLE_DIFF, mode="fix", file_contents={"stats.py": CURRENT},
                            model=model or DemoModel(), run_root=root, **kwargs)

    def test_parallel_failure_resumes_only_pending_agents(self):
        with tempfile.TemporaryDirectory() as root:
            failed = self.execute(root, ChangedModel(fail_security))
            self.assertEqual(failed["status"], "failed")
            item = next(item for item in failed["diagnostics"] if item["node"] == "security")
            self.assertEqual(item["stage"], "model_request")
            self.assertEqual(item["error_code"], "MODEL_CONNECTION_FAILED")
            self.assertTrue(item["locations"])
            resumed = resume_workflow(failed["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            starts = [event["node"] for event in resumed["trace_events"] if event["event"] == "node_started"]
            self.assertEqual(starts.count("bug_detector"), 1)
            self.assertEqual(starts.count("security"), 2)
            self.assertTrue(next(item for item in resumed["diagnostics"] if item["node"] == "security")["resolved"])
            self.assertEqual(resumed["run_id"], failed["run_id"])
            self.assertEqual(RunStore.open(root, failed["run_id"]).metadata["resumes"], 1)

    def test_all_parallel_failures_are_reported_and_resolve_after_resume(self):
        barrier = Barrier(2, timeout=10)

        def fail_two(role, data):
            if role in {"bug_detector", "security"}:
                barrier.wait()
                raise ConnectionError("Synthetic parallel failure")
            return data

        with tempfile.TemporaryDirectory() as root:
            failed = self.execute(root, ChangedModel(fail_two))
            self.assertEqual(sum(item["node"] in {"bug_detector", "security"}
                                 for item in failed["diagnostics"]), 2)
            for agent in ("bug_detector", "security"):
                self.assertTrue(any(item["node"] == agent and not item["resolved"] for item in failed["diagnostics"]))
                self.assertEqual(failed["reports"][agent].status, "failed")
                self.assertIn(f"- {agent}: failed", failed["final_review"])
                self.assertTrue(any(error.startswith(f"{agent}:") for error in failed["errors"]))
            resumed = resume_workflow(failed["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            for agent in ("bug_detector", "security"):
                self.assertEqual(resumed["reports"][agent].status, "completed")
                self.assertFalse(any(item["node"] == agent and not item["resolved"] for item in resumed["diagnostics"]))

    def test_abrupt_process_death_recovers_from_committed_checkpoint(self):
        script = '''import os,sys
sys.path.insert(0,sys.argv[1])
from examples.repair_demo import DemoModel,SAMPLE_DIFF,CURRENT
from src.workflow.service import run_workflow
class KilledModel(DemoModel):
    def invoke(self,messages):
        if messages[0].content.startswith('ROLE: patch_generator\\n'):
            os._exit(17)
        return super().invoke(messages)
run_workflow(SAMPLE_DIFF,mode='fix',model=KilledModel(),file_contents={'stats.py':CURRENT},run_root=sys.argv[2])
'''
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1]), root],
                                    env={"PATH": os.defpath}, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 17, result.stderr)
            run_id = next(Path(root).iterdir()).name
            self.assertEqual(RunStore.open(root, run_id).metadata["status"], "running")
            resumed = resume_workflow(run_id, run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            starts = [event["node"] for event in resumed["trace_events"] if event["event"] == "node_started"]
            self.assertEqual(starts.count("bug_detector"), 1)
            self.assertEqual(starts.count("patch_generator"), 2)
            self.assertTrue(any(item["error_code"] == "PROCESS_INTERRUPTED" and item["resolved"]
                                for item in resumed["diagnostics"]))

    def test_rejected_repair_resumes_with_new_attempt_without_repeating_review(self):
        with tempfile.TemporaryDirectory() as root:
            failed = self.execute(root, ChangedModel(invalid_patch), max_fix_attempts=1)
            self.assertEqual(failed["status"], "failed")
            diagnostic = next(item for item in failed["diagnostics"] if item["node"] == "validator")
            self.assertEqual(diagnostic["stage"], "python_syntax")
            self.assertEqual(diagnostic["file_path"], "stats.py")
            self.assertEqual(diagnostic["line"], 1)
            self.assertEqual(diagnostic["error_code"], "PYTHON_SYNTAX_INVALID")
            resumed = resume_workflow(failed["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            self.assertEqual(resumed["fix_attempts"], 2)
            starts = [event["node"] for event in resumed["trace_events"] if event["event"] == "node_started"]
            self.assertEqual(starts.count("bug_detector"), 1)
            self.assertEqual(starts.count("patch_generator"), 2)

    def test_resumed_repair_receives_previous_patch_and_failure_feedback(self):
        def reject_regression(role, data):
            if role == "regression_reviewer":
                data.update(status="failed", summary="The candidate still violates the empty-list contract.")
            return data

        class FeedbackModel(DemoModel):
            def __init__(self):
                self.feedback = []

            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: fix_planner\n"):
                    self.feedback.append(json.loads(messages[1].content)["previous_attempt"])
                return super().invoke(messages)

        for change in (invalid_patch, reject_regression):
            with self.subTest(failure=change.__name__), tempfile.TemporaryDirectory() as root:
                failed = self.execute(root, ChangedModel(change), max_fix_attempts=1)
                self.assertEqual(failed["status"], "failed")
                model = FeedbackModel()
                planner_inputs = []

                def plan(state):
                    planner_inputs.append((state["patch"], state["test_patch"], state["candidate_files"],
                                           state["validation"].status, state["regression"].status))
                    from src.repair.nodes import fix_planner_node
                    return fix_planner_node(state, model)

                resumed = resume_workflow(failed["run_id"], run_root=root, model=model,
                                          handlers={"fix_planner": plan})
                self.assertEqual(model.feedback, [{
                    "patch": failed["patch"].diff,
                    "validation": failed["validation"].summary,
                    "regression": failed["regression"].summary,
                    "diagnosis": failed.get("failure_diagnosis", {}),
                }])
                self.assertEqual(planner_inputs, [(None, None, {}, "not_run", "not_run")])
                self.assertEqual(resumed["status"], "proposed")
                self.assertEqual(resumed["fix_attempts"], 2)

    def test_resume_rejects_legacy_mismatched_sources_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunStore.create(root, {
                "code_diff": SAMPLE_DIFF, "file_paths": ["stats.py"], "file_contents": {"stats.py": BEFORE},
                "mode": "fix", "active_agents": list(AGENT_IDS), "max_fix_attempts": 1, "scaffold": False,
            })
            with patch("src.llm.get_fast_llm") as initialize:
                with self.assertRaisesRegex(ValueError, "post-change"):
                    resume_workflow(store.run_id, run_root=root)
            initialize.assert_not_called()
            self.assertEqual(RunStore.open(root, store.run_id).metadata["resumes"], 0)
            self.assertFalse((store.path/"checkpoint.sqlite").exists())

    def test_json_schema_failure_has_exact_phase_and_no_raw_response(self):
        class Malformed(DemoModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: security\n"):
                    return AIMessage(content='{"summary": 123, "findings": [], "extra": "SENSITIVE_TEST_MARKER"}')
                return super().invoke(messages)
        with tempfile.TemporaryDirectory() as root:
            failed = self.execute(root, Malformed())
            diagnostic = next(item for item in failed["diagnostics"] if item["node"] == "security")
            self.assertEqual(diagnostic["stage"], "schema_validation")
            self.assertEqual(diagnostic["error_code"], "MODEL_SCHEMA_INVALID")
            self.assertTrue(any(item["event"] == "schema_issues" for item in failed["trace_events"]))
            for path in Path(failed["run_dir"]).rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"SENSITIVE_TEST_MARKER", path.read_bytes(), str(path))

    def test_runtime_error_does_not_persist_exception_message_or_locals(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root, ChangedModel(fail_security))
            for path in Path(result["run_dir"]).rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"SENSITIVE_TEST_MARKER", path.read_bytes())

    def test_success_and_failure_classify_artifacts_separately(self):
        with tempfile.TemporaryDirectory() as root:
            good = self.execute(root)
            bad = self.execute(root, ChangedModel(invalid_patch), max_fix_attempts=1)
            for result, expected in ((good, True), (bad, False)):
                store = RunStore.open(root, result["run_id"])
                self.assertEqual(store.metadata["candidate_valid"], expected)
                self.assertEqual((store.path/"candidates/repair.diff").exists(), expected)
                self.assertTrue(any(item["classification"] == "draft" for item in store.metadata["artifacts"].values()))
            self.assertNotEqual(good["run_id"], bad["run_id"])

    def test_original_and_normalized_drafts_remain_addressable_by_event_hash(self):
        import re
        def bad_counts(role, data):
            if role == "patch_generator":
                data["diff"] = re.sub(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@ -9,99 +9,99 @@", data["diff"])
            return data
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root, ChangedModel(bad_counts))
            self.assertEqual(result["status"], "proposed")
            paths = []
            for event in result["trace_events"]:
                if event["event"] == "draft_saved":
                    for artifact in event["artifacts"]:
                        path = Path(result["run_dir"])/artifact["path"]
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])
                        paths.append(path)
            raw = next(path for path in paths if path.parent.name == "patch_generator")
            self.assertIn("-9,99", raw.read_text())
            self.assertNotIn("-9,99", result["patch"].diff)

    def test_corrupted_checkpoint_produces_safe_checkpoint_diagnostic(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root, ChangedModel(fail_security))
            (Path(result["run_dir"])/"checkpoint.sqlite").write_bytes(b"invalid database")
            resumed = resume_workflow(result["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "failed")
            self.assertTrue(any(item["error_code"] == "CHECKPOINT_READ_FAILED" for item in resumed["diagnostics"]))

    def test_publication_checks_hashes_before_changing_any_alias(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(Path(root)/"runs")
            store = RunStore.open(Path(root)/"runs", result["run_id"])
            destination = Path(root)/"output"
            destination.mkdir()
            (destination/"report.md").write_text("keep previous report")
            (store.path/"candidates/repair.diff").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "integrity"):
                publish_run(store, report_path=destination/"report.md", patch_path=destination/"repair.diff",
                            test_path=destination/"tests.diff", output_root=destination)
            self.assertEqual((destination/"report.md").read_text(), "keep previous report")

    def test_export_paths_cannot_collide_with_publication_indexes_or_lock(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            result = self.execute(root/"runs")
            store = RunStore.open(root/"runs", result["run_id"])
            destination = root/"output"
            destination.mkdir()
            previous = {}
            for name in ("report.md", "latest.json", "latest-review.json", "latest-fix.json", ".publish.lock"):
                previous[name] = "Previous " + name
                (destination/name).write_text(previous[name])
            for name in ("latest.json", "latest-review.json", "latest-fix.json", ".publish.lock"):
                for key in ("report_path", "patch_path", "test_path"):
                    with self.subTest(name=name, output=key):
                        paths = dict(report_path=destination/"report.md", patch_path=destination/"repair.diff",
                                     test_path=destination/"tests.diff")
                        paths[key] = destination/name
                        with self.assertRaisesRegex(ValueError, "reserved"):
                            publish_run(store, **paths, output_root=destination)
                        for path, content in previous.items():
                            self.assertEqual((destination/path).read_text(), content)

    def test_source_collision_protection_survives_a_different_publication_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            with chdir(root):
                result = run_workflow(SAMPLE_DIFF, model=DemoModel(), run_root=root/"runs")
            store = RunStore.open(root/"runs", result["run_id"])
            destination = root/"output"
            destination.mkdir()
            with chdir(destination), self.assertRaisesRegex(ValueError, "must not overwrite"):
                publish_run(store, report_path=root/"stats.py", patch_path=destination/"repair.diff",
                            test_path=destination/"tests.diff", output_root=destination)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_legacy_execution_identifier_can_still_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.execute(root/"runs")
            store = RunStore.open(root/"runs", result["run_id"])
            store.metadata["session_id"] = store.metadata.pop("execution_id")
            # Reproduce the old on-disk format, before Session redaction existed.
            (store.path/"manifest.json").write_text(json.dumps(store.metadata))
            destination = root/"output"
            latest = publish_run(store, report_path=destination/"report.md", patch_path=destination/"repair.diff",
                                 test_path=destination/"tests.diff", output_root=destination)
            self.assertTrue(latest["candidate_valid"])

    def test_legacy_interrupted_trace_keeps_execution_provenance(self):
        from src.runtime.tracing import TraceRecorder
        tracer = TraceRecorder("synthetic-run")
        tracer.events = [{"sequence": 1, "event": "node_started", "span_id": "synthetic-span", "attempt": 0,
                          "node": "security", "finding_ids": [], "session_id": "synthetic-previous-execution"}]
        tracer.record_interrupted_spans()
        event = tracer.events[-1]
        self.assertEqual(event["previous_execution_id"], "synthetic-previous-execution")
        self.assertEqual(event["execution_id"], tracer.session_id)

    def test_finished_checkpoint_can_be_republished_after_finalization_crash(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root)
            store = RunStore.open(root, result["run_id"])
            store.metadata["status"] = "running"
            store.save_metadata()
            resumed = resume_workflow(result["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            starts = [event for event in resumed["trace_events"] if event["event"] == "node_started" and event["node"] == "patch_generator"]
            self.assertEqual(len(starts), 1)

    def test_code_changes_and_completed_runs_require_explicit_handling(self):
        with tempfile.TemporaryDirectory() as root:
            failed = self.execute(root, ChangedModel(fail_security))
            with patch("src.workflow.service.code_fingerprint", return_value="changed"):
                with self.assertRaisesRegex(ValueError, "code changed"):
                    resume_workflow(failed["run_id"], run_root=root, model=DemoModel())
                recovered = resume_workflow(failed["run_id"], run_root=root, model=DemoModel(), allow_code_change=True)
            self.assertEqual(recovered["status"], "proposed")
            with self.assertRaisesRegex(ValueError, "already completed"):
                resume_workflow(failed["run_id"], run_root=root, model=DemoModel())

    def test_concurrent_resume_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root, ChangedModel(fail_security))
            store = RunStore.open(root, result["run_id"])
            with file_lock(store.path/".run.lock"), self.assertRaisesRegex(ValueError, "already active"):
                resume_workflow(result["run_id"], run_root=root, model=DemoModel())

    def test_corrupted_inputs_are_rejected_and_partial_trace_tail_is_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.execute(root, ChangedModel(fail_security))
            store = RunStore.open(root, result["run_id"])
            with (store.path/"events.jsonl").open("a") as stream:
                stream.write('{"partial":')
            previous = (store.path/"events.jsonl").read_bytes()
            store.read_events()
            self.assertEqual((store.path/"events.jsonl").read_bytes(), previous)
            resumed = resume_workflow(result["run_id"], run_root=root, model=DemoModel())
            self.assertEqual(resumed["status"], "proposed")
            for line in (store.path/"events.jsonl").read_text().splitlines():
                json.loads(line)
            inputs = store.read_inputs()
            inputs["mode"] = "review"
            write_json(store.path/"input.json", inputs)
            with self.assertRaisesRegex(ValueError, "integrity"):
                store.read_inputs()


class ArtifactCliTests(unittest.TestCase):
    def invoke(self, root, model, args=None):
        with chdir(root), patch("sys.argv", ["code-review", "--file", "input.diff", "--source-root", ".", "--mode", "fix", *(args or [])]), patch("src.llm.get_fast_llm", return_value=model), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            try:
                main()
                return 0
            except SystemExit as exc:
                return exc.code

    def test_clean_run_removes_stale_alias_but_preserves_history(self):
        def clean(role, data):
            return {"summary": "No supported issues.", "findings": []} if role in AGENT_IDS else data
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"stats.py").write_text(CURRENT)
            self.assertEqual(self.invoke(root, DemoModel()), 0)
            previous = json.loads((root/"output/latest.json").read_text())
            self.assertTrue((root/"output/repair.diff").exists())
            self.assertEqual(self.invoke(root, ChangedModel(clean)), 0)
            current = json.loads((root/"output/latest.json").read_text())
            self.assertNotEqual(current["run_id"], previous["run_id"])
            self.assertFalse(current["candidate_valid"])
            self.assertFalse((root/"output/repair.diff").exists())
            self.assertFalse((root/"output/tests.diff").exists())
            self.assertTrue((Path(previous["run_dir"])/"candidates/repair.diff").exists())

    def test_failure_exports_report_and_drafts_without_valid_candidate_alias(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"stats.py").write_text(CURRENT)
            self.assertEqual(self.invoke(root, DemoModel()), 0)
            self.assertEqual(self.invoke(root, ChangedModel(invalid_patch), ["--max-fix-attempts", "1"]), 1)
            latest = json.loads((root/"output/latest.json").read_text())
            self.assertEqual(latest["status"], "failed")
            self.assertNotIn("repair.diff", latest["exports"])
            self.assertFalse((root/"output/repair.diff").exists())
            self.assertTrue(list((Path(latest["run_dir"])/"drafts").rglob("repair.diff")))

    def test_modified_previous_patch_is_preserved_and_not_published_as_current(self):
        for filename in ("repair.diff", "tests.diff"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"input.diff").write_text(SAMPLE_DIFF)
                (root/"stats.py").write_text(CURRENT)
                self.assertEqual(self.invoke(root, DemoModel()), 0)
                target = root/"output"/filename
                target.write_text("User changed this file after publication\n")
                self.assertEqual(self.invoke(root, ChangedModel(invalid_patch), ["--max-fix-attempts", "1"]), 1)
                self.assertEqual(target.read_text(), "User changed this file after publication\n")
                latest = json.loads((root/"output/latest.json").read_text())
                self.assertNotIn(filename, latest["exports"])
                self.assertFalse(latest["candidate_valid"])
                self.assertEqual(latest["preserved_outputs"], [{"path": str(target.resolve()), "reason": "ownership_or_content_unverified"}])

    def test_invalid_or_missing_ownership_metadata_never_authorizes_cleanup(self):
        for metadata in ("not json", "[]", '{"schema_version": 1, "exports": []}'):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"input.diff").write_text(SAMPLE_DIFF)
                (root/"stats.py").write_text(CURRENT)
                (root/"output").mkdir()
                target = root/"output/repair.diff"
                target.write_text("Unowned existing content\n")
                (root/"output/latest.json").write_text(metadata)
                self.assertEqual(self.invoke(root, DemoModel(), ["--mode", "review"]), 0)
                self.assertEqual(target.read_text(), "Unowned existing content\n")

    def test_same_patch_content_at_an_unowned_path_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"stats.py").write_text(CURRENT)
            self.assertEqual(self.invoke(root, DemoModel()), 0)
            unrelated = root/"saved_by_user.diff"
            content = (root/"output/repair.diff").read_bytes()
            unrelated.write_bytes(content)
            self.assertEqual(self.invoke(root, DemoModel(), ["--mode", "review", "--patch-output", str(unrelated)]), 0)
            self.assertEqual(unrelated.read_bytes(), content)
            latest = json.loads((root/"output/latest.json").read_text())
            self.assertNotIn("repair.diff", latest["exports"])
            self.assertEqual(latest["preserved_outputs"][0]["path"], str(unrelated.resolve()))

    def test_unchanged_owned_custom_patch_outputs_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"stats.py").write_text(CURRENT)
            code, tests = root/"custom/code.diff", root/"custom/tests.diff"
            options = ["--patch-output", str(code), "--test-output", str(tests)]
            self.assertEqual(self.invoke(root, DemoModel(), options), 0)
            previous = json.loads((root/"output/latest.json").read_text())
            self.assertTrue(code.exists())
            self.assertTrue(tests.exists())
            self.assertEqual(self.invoke(root, DemoModel(), [*options, "--mode", "review"]), 0)
            self.assertFalse(code.exists())
            self.assertFalse(tests.exists())
            self.assertTrue((Path(previous["run_dir"])/"candidates/repair.diff").exists())
            self.assertTrue((Path(previous["run_dir"])/"candidates/tests.diff").exists())

    def test_inspect_filters_exact_agent_phase_without_initializing_model(self):
        with tempfile.TemporaryDirectory() as root:
            result = run_workflow(SAMPLE_DIFF, model=ChangedModel(fail_security), run_root=Path(root)/"runs")
            output = StringIO()
            with patch("sys.argv", ["code-review", "--inspect", result["run_id"], "--run-root", str(Path(root)/"runs"), "--node", "security", "--phase", "model_request"]), patch("src.llm.get_fast_llm", side_effect=AssertionError("No model calls")), redirect_stdout(output):
                main()
            data = json.loads(output.getvalue())
            self.assertTrue(data["events"])
            self.assertTrue(all(event["node"] == "security" and event["stage"] == "model_request" for event in data["events"]))

    def test_cli_resume_preserves_saved_mode_and_run_identity(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            result = run_workflow(SAMPLE_DIFF, mode="fix", model=ChangedModel(fail_security),
                                  file_contents={"stats.py": CURRENT}, run_root=root/"runs")
            with chdir(root), patch("sys.argv", ["code-review", "--resume", result["run_id"], "--run-root", str(root/"runs")]), patch("src.llm.get_fast_llm", return_value=DemoModel()), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main()
            latest = json.loads((root/"output/latest.json").read_text())
            self.assertEqual(latest["run_id"], result["run_id"])
            self.assertEqual(latest["mode"], "fix")
            self.assertTrue((root/"output/fix-review.md").exists())

    def test_unvalidated_proposal_stays_draft_and_legacy_outputs_are_archived(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/"input.diff").write_text(SAMPLE_DIFF)
            (root/"output").mkdir()
            (root/"output/repair.diff").write_text("legacy patch")
            with chdir(root), patch("sys.argv", ["code-review", "--file", "input.diff", "--mode", "fix"]), patch("src.llm.get_fast_llm", return_value=DemoModel()), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                main()
            latest = json.loads((root/"output/latest.json").read_text())
            self.assertEqual(latest["status"], "proposed")
            self.assertFalse(latest["candidate_valid"])
            self.assertEqual((root/"output/repair.diff").read_text(), "legacy patch")
            legacy = list((root/"output/legacy").glob("*/repair.diff"))
            self.assertEqual(len(legacy), 1)
            self.assertEqual(legacy[0].read_text(), "legacy patch")
