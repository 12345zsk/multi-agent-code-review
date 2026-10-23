"""Natural-language write decisions inside an explicit source boundary."""

import json
from pathlib import Path
import tempfile
import unittest

from langchain_core.messages import AIMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel
from src.workflow.registry import AGENT_IDS
from src.workflow.service import run_file_workflow


class IntentModel(DemoModel):
    def __init__(self, *, mode="fix", policy="required", outcome="accepted", clean=False):
        self.mode, self.policy, self.outcome, self.clean = mode, policy, outcome, clean
        self.calls = []

    def invoke(self, messages):
        role = messages[0].content.splitlines()[0].removeprefix("ROLE: ")
        payload = json.loads(messages[1].content)
        self.calls.append((role, payload))
        if role == "task_interpreter":
            admitted = self.outcome == "accepted"
            return AIMessage(content=json.dumps({
                "status": self.outcome, "mode": self.mode if admitted else None,
                "agents": ["bug_detector"] if admitted else None,
                "guidance": "按用户要求审查本地文件并保持原有契约。" if admitted else None,
                "write_policy": self.policy if admitted else None,
                "unsupported_requests": ["部署上线"] if self.outcome == "unsupported" else [],
                "reason": "任务范围明确。" if admitted else "任务不支持或需要澄清。",
            }, ensure_ascii=False))
        if self.clean and role in AGENT_IDS:
            return AIMessage(content=json.dumps({"summary": "No defects.", "findings": []}))
        return super().invoke(messages)


class AutoWriteTests(unittest.TestCase):
    def execute(self, root, model, task, **options):
        return run_file_workflow("stats.py", source_root=root, model=model, task=task,
                                 run_root=root/"runs", **options)

    def test_fix_goal_enables_writing_and_backs_up_without_a_write_parameter(self):
        for policy in ("required", "optional"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                model = IntentModel(policy=policy)
                result = self.execute(root, model, "检查这个文件，如果有错误，进行修正。")
                self.assertEqual(result["status"], "proposed")
                self.assertEqual(result["write_result"]["status"], "written")
                self.assertEqual((root/"stats.py").read_text(), BEFORE)
                entry = next(item for item in result["write_result"]["files"] if item["file_path"] == "stats.py")
                self.assertEqual((Path(result["run_dir"])/entry["backup"]).read_text(), CURRENT)
                inputs = json.loads((Path(result["run_dir"])/"input.json").read_text())
                manifest = json.loads((Path(result["run_dir"])/"manifest.json").read_text())
                self.assertIsNone(inputs["write_root"])
                self.assertEqual(inputs["auto_write_root"], str(root.resolve()))
                self.assertEqual(manifest["write_root"], str(root.resolve()))
                self.assertTrue(model.calls[0][1]["writes_available"])

    def test_review_and_candidate_only_goals_never_enter_writer(self):
        cases = (("review", "optional", "只检查这个文件，给出问题清单。", "completed"),
                 ("fix", "forbidden", "生成修复补丁，不要修改本地文件。", "proposed"))
        for mode, policy, task, status in cases:
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                result = self.execute(root, IntentModel(mode=mode, policy=policy), task)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["write_root"])
                self.assertEqual(result["write_result"]["status"], "not_run")
                self.assertEqual((root/"stats.py").read_text(), CURRENT)
                self.assertFalse((Path(result["run_dir"])/"writes").exists())
                self.assertFalse(any(event["node"] == "file_writer" for event in result["trace_events"]))

    def test_ambiguous_and_unsupported_requests_do_not_enable_writes(self):
        for outcome in ("unsupported", "needs_clarification"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"stats.py").write_text(CURRENT)
                model = IntentModel(outcome=outcome)
                result = self.execute(root, model, "处理一下或者部署这个项目。")
                self.assertEqual(result["status"], outcome)
                self.assertIsNone(result["write_root"])
                self.assertEqual(result["reports"], {})
                self.assertEqual(len(model.calls), 1)
                self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_missing_persistent_backup_location_stops_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            model = IntentModel()
            result = run_file_workflow("stats.py", source_root=root, model=model, task="修正这个文件。")
            self.assertEqual(result["status"], "needs_clarification")
            self.assertIn("run_root", result["final_review"])
            self.assertIsNone(result["write_root"])
            self.assertEqual(len(model.calls), 1)
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_api_can_explicitly_disable_automatic_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = self.execute(root, IntentModel(), "修正并写回这个文件。", write_files=False)
            self.assertEqual(result["status"], "needs_clarification")
            self.assertIsNone(result["write_root"])
            self.assertEqual((root/"stats.py").read_text(), CURRENT)

    def test_clean_file_is_not_rewritten_even_when_repair_was_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = self.execute(root, IntentModel(clean=True), "有错误就修正这个文件。")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["write_result"]["status"], "not_run")
            self.assertIn("No confirmed fixable findings", result["final_review"])
            self.assertNotIn("Interrupted writes may be partial", result["final_review"])
            self.assertEqual((root/"stats.py").read_text(), CURRENT)
            self.assertFalse((Path(result["run_dir"])/"writes").exists())

    def test_conflicting_explicit_mode_does_not_enable_automatic_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"stats.py").write_text(CURRENT)
            result = self.execute(root, IntentModel(), "修正这个文件。", mode="review")
            self.assertEqual(result["status"], "needs_clarification")
            self.assertIsNone(result["write_root"])
            self.assertEqual(result["reports"], {})
            self.assertEqual((root/"stats.py").read_text(), CURRENT)


if __name__ == "__main__":
    unittest.main()
