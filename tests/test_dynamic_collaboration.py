"""Dynamic routing, multi-file context, disputes and failure diagnosis without provider calls."""

import json
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import AIMessage, ToolMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, unified
from src.diff import parse_diff
from src.project_tools import ProjectContext, make_project_tools, project_inventory
from src.serialization import to_json
from src.workflow.service import resume_workflow, run_file_workflow, run_task_workflow, run_workflow


def reply(data):
    return AIMessage(content=json.dumps(data))


def tool_request(name, args, index=0):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"ctx-{index}", "type": "tool_call"}])


class DynamicModel(DemoModel):
    agents = ("bug_detector",)

    def __init__(self, targets=None, mode="review"):
        self.targets, self.mode, self.calls = targets or ["stats.py"], mode, []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        self.calls.append((role, payload))
        if role == "task_interpreter":
            data = {"status": "accepted", "mode": self.mode, "agents": None,
                    "guidance": "检查并修正确认的空输入缺陷，保留正常行为。", "write_policy": "optional",
                    "unsupported_requests": [], "reason": "明确的项目范围任务。"}
            if payload["resolve_target"]:
                data.update(target=None, targets=self.targets)
            return reply(data)
        if role == "review_planner":
            return reply({"assignments": [{"agent_id": agent, "reason": "Visible input contract and caller risk.",
                                            "questions": ["Does the empty-list contract hold for the visible caller?"]}
                                           for agent in self.agents]})
        data = json.loads(super().invoke(messages).content)
        if role == "bug_detector":
            target = next(p for p in payload["file_paths"] if p.endswith("stats.py"))
            data["findings"][0]["file_path"] = target
        return reply(data)


class DynamicRoutingTests(unittest.TestCase):
    def test_model_plan_alone_controls_dispatch_and_is_reported(self):
        model = DynamicModel()
        result = run_workflow(SAMPLE_DIFF, model=model, file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["active_agents"], ["bug_detector"])
        self.assertEqual(list(result["reports"]), ["bug_detector"])
        assignment = next(p for r, p in model.calls if r == "bug_detector")["assignment"]
        self.assertTrue(assignment["questions"])
        self.assertIn("Dynamic review plan", result["final_review"])
        with self.assertRaisesRegex(ValueError, "dynamic"):
            run_workflow(SAMPLE_DIFF, active_agents=["security"], model=model)

    def test_invalid_plan_never_falls_back_to_all_experts(self):
        class Invalid(DynamicModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: review_planner\n"):
                    return reply({"assignments": []})
                return super().invoke(messages)
        result = run_workflow(SAMPLE_DIFF, model=Invalid())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reports"], {})
        self.assertEqual(result["active_agents"], [])

    def test_multiple_explicit_targets_and_directory_requests(self):
        for references, task in [(["stats.py", "helpers.py"], "审查 stats.py 和 helpers.py"), (["pkg/"], "审查 pkg/ 目录")]:
            with self.subTest(references=references), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                base = root/"pkg" if references == ["pkg/"] else root
                base.mkdir(exist_ok=True)
                (base/"stats.py").write_text(CURRENT)
                (base/"helpers.py").write_text("from stats import average\nvalue = average([])\n")
                result = run_task_workflow(task, source_root=root, run_root=root/"output/runs", model=DynamicModel(references))
                self.assertEqual(result["status"], "completed", result["errors"])
                self.assertEqual(len(result["file_paths"]), 2)
                self.assertEqual(len(result["source_hashes"]), 2)
                self.assertEqual(len(result["target_files"]), 2)
                self.assertEqual((base/"stats.py").read_text(), CURRENT)

    def test_invented_or_external_multi_targets_stop_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            for refs in (["stats.py", "invented.py"], ["stats.py", "../outside.py"]):
                result = run_task_workflow("检查 stats.py 和 ../outside.py", source_root=root,
                                           run_root=root/"output/runs", model=DynamicModel(refs))
                self.assertEqual(result["status"], "needs_clarification")
                self.assertEqual(result["reports"], {})

    def test_multi_file_repair_writes_only_explicit_targets_with_individual_backups(self):
        helper = 'def constant():\n    """Return zero."""\n    return 1/0\n'
        repaired = helper.replace("return 1/0", "return 0")
        class Multi(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                data = json.loads(super().invoke(messages).content)
                if role == "bug_detector":
                    data["findings"].append({"category": "other", "title": "Constant violates return contract",
                        "description": "The documented zero result instead always raises ZeroDivisionError.",
                        "file_path": "helpers.py", "line": 3, "severity": "high", "fixable": True,
                        "suggestion": "Return zero as documented.", "evidence": "    return 1/0"})
                elif role == "patch_generator":
                    data["diff"] = unified(CURRENT, BEFORE, "stats.py") + unified(helper, repaired, "helpers.py")
                elif role == "test_generator":
                    data = {"status": "not_needed", "diff": "", "summary": "Synthetic multi-file fixture; independent runtime acceptance remains external."}
                return reply(data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"stats.py": CURRENT, "helpers.py": helper}
            for name, content in original.items():
                (root/name).write_text(content)
            (root/"unrelated.py").write_text("unchanged = True\n")
            result = run_task_workflow("检查 stats.py 和 helpers.py，有错误就修正", source_root=root,
                                       run_root=root/"output/runs", model=Multi(list(original), mode="fix"))
            self.assertEqual(result["status"], "proposed", result["errors"])
            self.assertEqual(result["write_result"]["status"], "written")
            self.assertEqual((root/"stats.py").read_text(), BEFORE)
            self.assertEqual((root/"helpers.py").read_text(), repaired)
            self.assertEqual((root/"unrelated.py").read_text(), "unchanged = True\n")
            self.assertEqual(len(result["write_result"]["files"]), 2)
            for item in result["write_result"]["files"]:
                self.assertEqual((Path(result["run_dir"])/item["backup"]).read_text(), original[item["file_path"]])


class ProjectToolTests(unittest.TestCase):
    def test_lazily_read_target_must_match_the_reviewed_diff_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(BEFORE)
            context = ProjectContext({}, root=root, inventory=project_inventory(root), review_diff=SAMPLE_DIFF)
            with self.assertRaisesRegex(ValueError, "SOURCE_DIFF_MISMATCH"):
                context.read("stats.py")
            self.assertNotIn("stats.py", context.sources)
            (root/"stats.py").write_text(CURRENT)
            context = ProjectContext({}, root=root, inventory=project_inventory(root), review_diff=SAMPLE_DIFF)
            self.assertEqual(context.read("stats.py"), CURRENT)

    def test_discovery_search_and_ast_use_real_related_files_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"helpers.py").write_text("from stats import average\nvalue = average([])\nraise RuntimeError('must not execute')\n")
            (root/".env").write_text("SYNTHETIC_PRIVATE_MARKER")
            (root/"private.key").write_text("SYNTHETIC_PRIVATE_MARKER")
            (root/"linked.py").symlink_to(root/"helpers.py")
            inventory = project_inventory(root)
            self.assertEqual(list(inventory), ["helpers.py"])
            context = ProjectContext({}, root=root, inventory=inventory)
            tools = make_project_tools(context)
            self.assertEqual(tools["list_project_files"].invoke({})["files"], ["helpers.py"])
            matches = tools["search_code"].invoke({"query": "average"})["matches"]
            self.assertEqual(matches[1]["line"], 2)
            self.assertTrue(tools["analyze_python"].invoke({"file_path": "helpers.py"})["syntax_valid"])
            self.assertEqual(tools["read_source"].invoke({"file_path": "helpers.py"})["status"], "ok")
            for path in (".env", "private.key", "linked.py"):
                self.assertEqual(tools["read_source"].invoke({"file_path": path})["status"], "error")
            with self.assertRaises(ValueError):
                tools["read_source"].invoke({"file_path": "../outside.py"})

    def test_snapshot_is_reused_but_unread_changed_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"helpers.py").write_text("original = True\n")
            inventory = project_inventory(root)
            run_dir = root/"output/runs/example"
            run_dir.mkdir(parents=True)
            context = ProjectContext({}, root=root, inventory=inventory, run_dir=run_dir)
            self.assertEqual(context.read("helpers.py"), "original = True\n")
            (root/"helpers.py").write_text("changed = True\n")
            restored = ProjectContext({}, root=root, inventory=inventory, run_dir=run_dir)
            self.assertEqual(restored.read("helpers.py"), "original = True\n")
            fresh = ProjectContext({}, root=root, inventory=inventory)
            with self.assertRaisesRegex(ValueError, "SOURCE_VERSION_CHANGED"):
                fresh.read("helpers.py")

    def test_related_file_observations_reach_adjudication_but_not_public_json(self):
        class Reader(DynamicModel):
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    observations = [m for m in messages if isinstance(m, ToolMessage)]
                    actions = [("list_project_files", {}), ("search_code", {"query": "average"}),
                               ("read_source", {"file_path": "helpers.py"}), ("analyze_python", {"file_path": "helpers.py"})]
                    if len(observations) < len(actions):
                        name, args = actions[len(observations)]
                        return tool_request(name, args, len(observations))
                return super().invoke(messages)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"helpers.py").write_text("# PRIVATE_CONTEXT_MARKER\nfrom stats import average\nvalue = average([])\n")
            model = Reader()
            result = run_file_workflow("stats.py", source_root=root, run_root=root/"output/runs", model=model)
            self.assertEqual(result["status"], "completed", result["errors"])
            payload = next(p for role, p in model.calls if role == "adjudicator")
            self.assertIn("helpers.py", payload["sources"])
            self.assertNotIn("PRIVATE_CONTEXT_MARKER", to_json(result))
            self.assertEqual(result["file_paths"], ["stats.py"])
            self.assertEqual({e["tool"] for e in result["trace_events"] if e["event"] == "tool_completed"},
                             {"list_project_files", "search_code", "read_source", "analyze_python"})
            self.assertEqual(sum(e["event"] == "react_step" and e["node"] == "bug_detector" for e in result["trace_events"]), 5)

    def test_resume_retains_related_snapshot_after_live_file_changes(self):
        class Reader(DynamicModel):
            def __init__(self, interrupt=False):
                super().__init__()
                self.interrupt = interrupt
            def invoke(self, messages):
                if messages[0].content.startswith("ROLE: bug_detector\n"):
                    if not any(isinstance(m, ToolMessage) for m in messages):
                        return tool_request("read_source", {"file_path": "helpers.py"})
                    if self.interrupt:
                        raise ConnectionError("Synthetic interruption")
                return super().invoke(messages)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            original = "from stats import average\nvalue = average([])\n"
            (root/"helpers.py").write_text(original)
            failed = run_file_workflow("stats.py", source_root=root, run_root=root/"output/runs", model=Reader(True))
            self.assertEqual(failed["status"], "failed")
            (root/"helpers.py").write_text("changed = True\n")
            resumed = resume_workflow(failed["run_id"], run_root=root/"output/runs", model=Reader())
            self.assertEqual(resumed["status"], "completed", resumed["errors"])
            self.assertEqual(resumed["observed_sources"]["bug_detector"]["helpers.py"], original)
            self.assertEqual(sum(e["event"] == "tool_completed" for e in resumed["trace_events"]), 1)


class CollaborationTests(unittest.TestCase):
    def test_multiple_assigned_experts_checkpoint_in_one_review_round(self):
        class Multiple(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                data = json.loads(super().invoke(messages).content)
                if role == "adjudicator" and not payload["review_round"]:
                    data["disputes"] = [{"agent_id": agent, "finding_ids": [payload["candidates"][0]["id"]],
                                         "question": "Verify the caller and documented return contract."}
                                        for agent in ("bug_detector", "compatibility")]
                return reply(data)
        with tempfile.TemporaryDirectory() as directory:
            result = run_workflow(SAMPLE_DIFF, model=Multiple(), file_contents={"stats.py": CURRENT}, run_root=directory)
            self.assertEqual(result["status"], "completed", result["errors"])
            self.assertEqual(result["review_round"], 1)
            self.assertEqual(len(result["dispute_reviews"]), 2)

    def test_repeated_dispute_requests_exhaust_budget_without_auto_fix(self):
        class Repeated(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                data = json.loads(super().invoke(messages).content)
                if role == "adjudicator":
                    data["disputes"] = [{"agent_id": "bug_detector", "finding_ids": [payload["candidates"][0]["id"]],
                                         "question": "The intended contract remains unclear; verify it."}]
                return reply(data)
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=Repeated(), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["review_round"], 1)
        self.assertEqual(result["fix_attempts"], 0)
        self.assertEqual(result["findings"][0].status, "candidate")
        self.assertFalse(result["findings"][0].fixable)

    def test_uncertain_finding_automatically_gets_one_focused_recheck(self):
        class Uncertain(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                data = json.loads(super().invoke(messages).content)
                if role == "adjudicator":
                    data["decisions"][0]["status"] = "candidate"
                return reply(data)
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=Uncertain(), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["review_round"], 1)
        self.assertEqual(len(result["dispute_reviews"]), 1)
        self.assertEqual(result["fix_attempts"], 0)

    def test_context_changes_before_write_block_all_candidate_writes(self):
        class Concurrent(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                if role == "bug_detector" and not any(isinstance(m, ToolMessage) for m in messages):
                    return tool_request("read_source", {"file_path": "helpers.py"})
                if role == "regression_reviewer":
                    (root/"helpers.py").write_text("user_edit = True\n")
                return super().invoke(messages)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"helpers.py").write_text("from stats import average\nassert average([]) == 0\n")
            result = run_file_workflow("stats.py", source_root=root, mode="fix", model=Concurrent(),
                                       write_files=True, run_root=root/"output/runs")
            self.assertEqual(result["status"], "failed")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertFalse((root/"generated_tests").exists())
            self.assertTrue(any(d["error_code"] == "CONTEXT_VERSION_CHANGED" for d in result["diagnostics"]))

    def test_dispute_triggers_one_targeted_review_and_re_adjudication(self):
        class Disputed(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                if role == "dispute_reviewer":
                    self.calls.append((role, payload))
                    return reply({"resolutions": [{"finding_id": f["id"], "status": "confirmed",
                                                    "reason": "Exact source and documented empty-input contract.", "evidence": f["evidence"]}
                                                   for f in payload["candidates"]]})
                data = json.loads(super().invoke(messages).content)
                if role == "adjudicator" and not payload["review_round"]:
                    data["decisions"][0]["status"] = "candidate"
                    data["disputes"] = [{"agent_id": "compatibility", "finding_ids": [payload["candidates"][0]["id"]],
                                         "question": "Does the documented return contract prove this behavior is a defect?"}]
                return reply(data)
        model = Disputed()
        result = run_workflow(SAMPLE_DIFF, file_contents={"stats.py": CURRENT}, model=model)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review_round"], 1)
        self.assertEqual(sum(role == "adjudicator" for role, _ in model.calls), 2)
        self.assertEqual(sum(role == "dispute_reviewer" for role, _ in model.calls), 1)
        self.assertEqual(result["findings"][0].status, "confirmed")
        self.assertIn("Targeted dispute review", result["final_review"])

    def test_failure_diagnosis_collects_context_and_changes_retry_input(self):
        class Diagnosed(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                payload = json.loads(messages[1].content)
                if role == "failure_diagnoser":
                    if not any(isinstance(m, ToolMessage) for m in messages):
                        return tool_request("read_source", {"file_path": "helpers.py"})
                    return reply({"category": "context_missing", "action": "collect_context",
                                  "summary": "Read actual caller contract.", "instructions": "Restore zero-on-empty without changing normal input behavior."})
                data = json.loads(super().invoke(messages).content)
                if role == "patch_generator" and sum(r == role for r, _ in self.calls) == 1:
                    data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
                return reply(data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            (root/"helpers.py").write_text("from stats import average\nassert average([]) == 0\n")
            model = Diagnosed()
            result = run_file_workflow("stats.py", source_root=root, mode="fix", model=model)
            self.assertEqual(result["status"], "proposed", result["errors"])
            self.assertEqual(result["fix_attempts"], 2)
            plan = [p for role, p in model.calls if role == "fix_planner"][1]
            self.assertIn("helpers.py", plan["sources"])
            self.assertEqual(plan["previous_attempt"]["diagnosis"]["category"], "context_missing")
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_diagnoser_can_stop_instead_of_blind_retry(self):
        class Stop(DynamicModel):
            def invoke(self, messages):
                role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
                if role == "failure_diagnoser":
                    return reply({"category": "unsupported", "action": "stop", "summary": "Contract is insufficient.",
                                  "instructions": "Request human clarification of intended behavior."})
                data = json.loads(super().invoke(messages).content)
                if role == "patch_generator":
                    data["diff"] = unified(CURRENT, "def average(:\n", "stats.py")
                return reply(data)
        result = run_workflow(SAMPLE_DIFF, mode="fix", model=Stop(), file_contents={"stats.py": CURRENT}, max_fix_attempts=3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fix_attempts"], 1)
        self.assertEqual(result["failure_diagnosis"]["action"], "stop")


if __name__ == "__main__":
    unittest.main()
