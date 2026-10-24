"""Regression coverage for malformed model patches and unusable test output."""

import unittest
import json
import re

from langchain_core.messages import AIMessage

from examples.repair_demo import BEFORE, CURRENT, DemoModel, SAMPLE_DIFF, unified
from src.contracts import FixPlan, PatchArtifact
from src.diff import DiffError, apply_patch, parse_diff
from src.repair.nodes import validator_node
from src.repair.patches import canonicalize_patch
from src.workflow.service import run_workflow


class ModelPatchTests(unittest.TestCase):
    def test_wrong_counts_and_coordinates_are_anchored_to_exact_source(self):
        source = "heading\nfirst = 1\ngap\nlast = 2\nend\n"
        malformed = "--- a/example.py\n+++ b/example.py\n@@ -7,8 +9,9 @@\n-first = 1\n+first = 10\n@@ -20,6 +22,6 @@\n-last = 2\n+last = 20\n"
        canonical = canonicalize_patch(malformed, {"example.py": source})
        result = apply_patch({"example.py": source}, parse_diff(canonical))
        self.assertEqual(result["example.py"], "heading\nfirst = 10\ngap\nlast = 20\nend\n")

    def test_new_test_file_count_is_derived_from_actual_body(self):
        malformed = "--- /dev/null\n+++ b/tests/test_example.py\n@@ -0,0 +1,118 @@\n+import unittest\n+value = 1\n"
        canonical = canonicalize_patch(malformed, {})
        self.assertEqual(apply_patch({}, parse_diff(canonical))["tests/test_example.py"], "import unittest\nvalue = 1\n")

    def test_ambiguous_context_is_not_guessed(self):
        patch = "--- a/example.py\n+++ b/example.py\n@@ -20,1 +20,1 @@\n-repeat()\n+fixed()\n"
        with self.assertRaisesRegex(DiffError, "ambiguous"):
            canonicalize_patch(patch, {"example.py": "repeat()\nseparator\nrepeat()\n"})

    def test_missing_context_is_not_fuzzily_matched(self):
        patch = unified("value = 1\n", "value = 2\n", "example.py")
        with self.assertRaisesRegex(DiffError, "missing"):
            canonicalize_patch(patch, {"example.py": "value = 3\n"})

    def test_valid_coordinates_disambiguate_repeated_context(self):
        source = "repeat()\nseparator\nrepeat()\n"
        patch = "--- a/example.py\n+++ b/example.py\n@@ -3,1 +3,1 @@\n-repeat()\n+fixed()\n"
        canonical = canonicalize_patch(patch, {"example.py": source})
        self.assertEqual(apply_patch({"example.py": source}, parse_diff(canonical))["example.py"], "repeat()\nseparator\nfixed()\n")

    def test_no_final_newline_is_preserved(self):
        patch = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        canonical = canonicalize_patch(patch, {"example.py": "old"})
        self.assertEqual(apply_patch({"example.py": "old"}, parse_diff(canonical))["example.py"], "new")

    def test_missing_old_eof_marker_is_recovered_from_exact_source(self):
        source = "import\n\ndef main():\n    return 1/0"
        patch = "--- a/example.py\n+++ b/example.py\n@@ -1,4 +1,3 @@\n-import\n \n def main():\n-    return 1/0\n+    return 1\n"
        with self.assertRaises(DiffError):
            apply_patch({"example.py": source}, parse_diff(patch))
        canonical = canonicalize_patch(patch, {"example.py": source})
        self.assertIn("-    return 1/0\n\\ No newline at end of file\n", canonical)
        self.assertEqual(apply_patch({"example.py": source}, parse_diff(canonical))["example.py"],
                         "\ndef main():\n    return 1\n")
        self.assertEqual(canonicalize_patch(canonical, {"example.py": source}), canonical)

    def test_missing_marker_on_unchanged_eof_context_preserves_source_newline(self):
        source = "value = 1\ntail"
        patch = "--- a/example.py\n+++ b/example.py\n@@ -1,2 +1,2 @@\n-value = 1\n+value = 2\n tail\n"
        canonical = canonicalize_patch(patch, {"example.py": source})
        self.assertEqual(apply_patch({"example.py": source}, parse_diff(canonical))["example.py"], "value = 2\ntail")

    def test_eof_repair_does_not_hide_ambiguous_reanchoring(self):
        source = "repeat()\nseparator\nrepeat()"
        patch = "--- a/example.py\n+++ b/example.py\n@@ -20,1 +20,1 @@\n-repeat()\n+fixed()\n"
        with self.assertRaisesRegex(DiffError, "ambiguous"):
            canonicalize_patch(patch, {"example.py": source})
        canonical = canonicalize_patch(patch.replace("-20,1 +20,1", "-3,1 +3,1"), {"example.py": source})
        self.assertEqual(apply_patch({"example.py": source}, parse_diff(canonical))["example.py"],
                         "repeat()\nseparator\nfixed()\n")

    def test_eof_repair_does_not_relax_content_or_explicit_newline_checks(self):
        cases = [
            ("old ", "-old\n"),
            ("first\nlast", "-first\n\\ No newline at end of file\n-last\n"),
            ("old\n", "-old\n\\ No newline at end of file\n"),
        ]
        for source, body in cases:
            with self.subTest(source=source):
                patch = "--- a/example.py\n+++ b/example.py\n@@ -1,1 +1,1 @@\n" + body + "+new\n"
                with self.assertRaisesRegex(DiffError, "missing"):
                    canonicalize_patch(patch, {"example.py": source})

    def test_unknown_source_does_not_gain_fake_application_validation(self):
        patch = unified(CURRENT, BEFORE, "stats.py")
        result = validator_node({
            "patch": PatchArtifact("generated", patch, ("f",)),
            "test_patch": PatchArtifact("not_needed", summary="Source missing."),
            "fix_plan": FixPlan("ready", ("f",)), "file_paths": ["stats.py"], "file_contents": {},
        })
        self.assertEqual(result["validation"].status, "not_run")

    def test_deleted_file_and_multiple_new_files_remain_valid(self):
        patch = "--- a/old.py\n+++ /dev/null\n@@ -1,9 +0,0 @@\n-old\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,8 @@\n+new\n"
        baseline = {"old.py": "old\n"}
        self.assertEqual(apply_patch(baseline, parse_diff(canonicalize_patch(patch, baseline))), {"new.py": "new\n"})


class GeneratedTestValidationTests(unittest.TestCase):
    def validate(self, source, path="tests/test_stats.py", **options):
        return validator_node({
            "patch": PatchArtifact("generated", unified(CURRENT, BEFORE, "stats.py"), ("f",)),
            "test_patch": PatchArtifact("generated", unified("", source, path, new=True), ("f",)),
            "fix_plan": FixPlan("ready", ("f",)), "file_paths": ["stats.py"], "file_contents": {"stats.py": CURRENT},
            **options,
        })

    def test_new_test_paths_are_stable_per_run_before_model_review(self):
        tests = "import unittest\nclass Cases(unittest.TestCase):\n    def test_value(self):\n        self.assertTrue(True)\n"
        results = [self.validate(tests, write_root="unused", run_id=run_id) for run_id in ("run-one", "run-two")]
        paths = []
        for result, run_id in zip(results, ("run-one", "run-two")):
            self.assertEqual(result["validation"].status, "passed")
            path = parse_diff(result["test_patch"].diff)[0].path
            self.assertRegex(path, r"^generated_tests/test_stats__run_[a-f0-9]{12}\.py$")
            self.assertEqual(result["candidate_files"][path], tests)
            repeated = self.validate(tests, write_root="unused", run_id=run_id, test_patch=result["test_patch"])
            self.assertEqual(repeated["test_patch"], result["test_patch"])
            self.assertEqual(repeated["candidate_files"], result["candidate_files"])
            paths.append(path)
        self.assertNotEqual(paths[0], paths[1])

    def test_existing_test_edits_keep_the_snapshotted_path(self):
        original = "import unittest\nclass Cases(unittest.TestCase):\n    def test_value(self):\n        self.assertTrue(False)\n"
        updated = original.replace("False", "True")
        path = "tests/test_stats.py"
        result = self.validate(updated, write_root="unused", run_id="run-one",
                               file_paths=["stats.py", path],
                               file_contents={"stats.py": CURRENT, path: original},
                               test_patch=PatchArtifact("generated", unified(original, updated, path), ("f",)))
        self.assertEqual(result["validation"].status, "passed")
        self.assertEqual(parse_diff(result["test_patch"].diff)[0].path, path)
        self.assertEqual(result["candidate_files"][path], updated)

    def test_related_existing_tests_are_read_only_without_explicit_target_scope(self):
        original = "import unittest\nclass Cases(unittest.TestCase):\n    def test_value(self):\n        self.assertTrue(False)\n"
        path = "tests/test_stats.py"
        result = self.validate("", file_contents={"stats.py": CURRENT},
                               observed_sources={"bug_detector": {path: original}},
                               test_patch=PatchArtifact("generated", unified(original, original.replace("False", "True"), path), ("f",)))
        self.assertEqual(result["validation"].status, "failed")
        self.assertEqual(result["validation"].file_path, path)
        self.assertIn("read-only", result["validation"].summary)
        self.assertEqual(result["candidate_files"], {})
    def test_new_non_python_tests_also_use_the_generated_directory(self):
        patch = PatchArtifact("generated", unified("", "check();\n", "tests/regression.js", new=True), ("f",))
        result = self.validate("", test_patch=patch)
        self.assertEqual(result["validation"].status, "passed")
        self.assertEqual(result["candidate_files"]["generated_tests/regression.js"], "check();\n")

    def test_pytest_dependency_is_rejected(self):
        result = self.validate("import pytest\ndef test_empty():\n    assert True\n")
        self.assertEqual(result["validation"].status, "failed")
        self.assertIn("not pytest", result["validation"].summary)

    def test_plain_functions_are_not_accepted_as_discoverable_unittest(self):
        result = self.validate("def test_empty():\n    assert True\n")
        self.assertEqual(result["validation"].status, "failed")
        self.assertIn("TestCase", result["validation"].summary)

    def test_python_test_filename_must_be_discoverable(self):
        result = self.validate("import unittest\nclass Checks(unittest.TestCase):\n    def test_empty(self):\n        self.assertTrue(True)\n", "tests/stats_test.py")
        self.assertEqual(result["validation"].status, "failed")
        self.assertIn("test_*.py", result["validation"].summary)

    def test_unittest_aliases_are_supported_without_executing_tests(self):
        result = self.validate("from unittest import TestCase as Case\nclass Checks(Case):\n    def test_empty(self):\n        raise RuntimeError('must not execute')\n")
        self.assertEqual(result["validation"].status, "passed")
        self.assertIn("unittest_discovery:generated_tests/test_stats.py", result["validation"].checks)

    def test_live_workflow_exports_the_canonical_artifacts(self):
        class BadCountsModel(DemoModel):
            def invoke(self, messages):
                response = super().invoke(messages)
                role = messages[0].content.splitlines()[0]
                if role in {"ROLE: patch_generator", "ROLE: test_generator"}:
                    data = json.loads(response.content)
                    data["diff"] = re.sub(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@ -0,0 +1,118 @@" if role.endswith("test_generator") else "@@ -9,99 +9,99 @@", data["diff"])
                    return AIMessage(content=json.dumps(data))
                return response

        result = run_workflow(SAMPLE_DIFF, mode="fix", model=BadCountsModel(), file_contents={"stats.py": CURRENT})
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["fix_attempts"], 1)
        self.assertEqual(apply_patch({"stats.py": CURRENT}, parse_diff(result["patch"].diff))["stats.py"], BEFORE)
        self.assertIn("generated_tests/test_stats.py", apply_patch({}, parse_diff(result["test_patch"].diff)))
