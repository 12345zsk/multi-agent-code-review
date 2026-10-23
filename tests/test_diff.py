"""Unified diff validation and in-memory application, including boundary cases."""

import unittest

from examples.repair_demo import BEFORE, CURRENT, SAMPLE_DIFF, unified
from src.diff import DiffError, apply_patch, parse_diff, safe_path
from src.workflow.service import run_workflow


class DiffTests(unittest.TestCase):
    def test_round_trip_multiple_hunks_and_files(self):
        before = "".join(f"line {i}\n" for i in range(40))
        after = before.replace("line 2\n", "changed 2\n").replace("line 37\n", "changed 37\n")
        diff = unified(before, after, "a.py") + unified("x\n", "y\n", "b.py")
        changes = parse_diff(diff)
        self.assertEqual(len(changes[0].hunks), 2)
        self.assertEqual(apply_patch({"a.py": before, "b.py": "x\n"}, changes), {"a.py": after, "b.py": "y\n"})

    def test_new_file_and_empty_baseline(self):
        diff = unified("", "x = 1\n", "tests/test_a.py", new=True)
        self.assertEqual(apply_patch({}, parse_diff(diff)), {"tests/test_a.py": "x = 1\n"})
        with self.assertRaises(DiffError):
            apply_patch({"tests/test_a.py": "existing\n"}, parse_diff(diff))

    def test_delete_file(self):
        diff = "--- a/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
        self.assertEqual(apply_patch({"a.py": "x = 1\n"}, parse_diff(diff)), {})

    def test_insert_at_eof(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1,0 +2 @@\n+y\n"
        self.assertEqual(apply_patch({"a.py": "x\n"}, parse_diff(diff))["a.py"], "x\ny\n")

    def test_no_newline_marker(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        self.assertEqual(apply_patch({"a.py": "old"}, parse_diff(diff))["a.py"], "new")

    def test_quoted_path_with_spaces(self):
        diff = '--- "a/my file.py"\n+++ "b/my file.py"\n@@ -1 +1 @@\n-x\n+y\n'
        self.assertEqual(apply_patch({"my file.py": "x\n"}, parse_diff(diff))["my file.py"], "y\n")

    def test_git_octal_quoted_unicode_path(self):
        quoted = "".join("\\" + format(byte, "03o") for byte in "中文".encode("utf-8"))
        diff = f'--- "a/{quoted}.py"\n+++ "b/{quoted}.py"\n@@ -1 +1 @@\n-x\n+y\n'
        self.assertEqual(apply_patch({"中文.py": "x\n"}, parse_diff(diff))["中文.py"], "y\n")

    def test_invalid_counts_missing_headers_and_extra_lines(self):
        for diff in (
            "not a patch", "--- a/a.py\n", "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1 @@\n-x\n+y\n",
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n+extra\n",
        ):
            with self.subTest(diff=diff[:20]), self.assertRaises(DiffError):
                parse_diff(diff)

    def test_traversal_and_git_metadata_paths(self):
        for path in ("../a.py", "/tmp/a.py", ".git/config", "a/../../b", "C:/a.py", "a\\b", "."):
            with self.subTest(path=path), self.assertRaises(DiffError):
                safe_path(path)

    def test_context_mismatch_missing_source_and_wrong_target_position(self):
        diff = unified("x\n", "y\n", "a.py")
        with self.assertRaises(DiffError):
            apply_patch({"a.py": "z\n"}, parse_diff(diff))
        with self.assertRaises(KeyError):
            apply_patch({}, parse_diff(diff))
        with self.assertRaises(DiffError):
            apply_patch({"a.py": "x\n"}, parse_diff(diff.replace("+1 @@", "+2 @@")))

    def test_duplicate_sections_and_mixed_binary_are_rejected(self):
        diff = unified("x\n", "y\n", "a.py")
        for text in (diff + diff, diff + "Binary files a/image.png and b/image.png differ\n"):
            with self.assertRaises(DiffError):
                parse_diff(text)


class SourceContextTests(unittest.TestCase):
    def assert_rejected(self, diff, sources):
        with self.assertRaisesRegex(ValueError, "post-change"):
            run_workflow(diff, scaffold=True, file_contents=sources)

    def test_old_version_and_shifted_target_lines_are_rejected(self):
        for source in (BEFORE, "# inserted line\n" + CURRENT,
                       CURRENT.replace("return sum(values) / len(values)", "return 42")):
            with self.subTest(source=source):
                self.assert_rejected(SAMPLE_DIFF, {"stats.py": source})

    def test_multiple_hunks_validate_content_and_coordinates(self):
        before = "".join(f"line {i}\n" for i in range(40))
        after = before.replace("line 2\n", "changed 2\n").replace("line 37\n", "changed 37\n")
        diff = unified(before, after, "a.txt")
        result = run_workflow(diff, scaffold=True, file_contents={"a.txt": after})
        self.assertEqual(result["errors"], [])
        self.assert_rejected(diff, {"a.txt": after.replace("changed 37\n", "line 37\n")})

    def test_new_file_requires_the_complete_target(self):
        diff = unified("", "new\n", "a.txt", new=True)
        result = run_workflow(diff, scaffold=True, file_contents={"a.txt": "new\n"})
        self.assertEqual(result["errors"], [])
        for source in ("old\n", "new\nextra\n", "prefix\nnew\n"):
            with self.subTest(source=source):
                self.assert_rejected(diff, {"a.txt": source})

    def test_deleted_file_cannot_be_present_in_postchange_sources(self):
        diff = "--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        for source in ("old\n", ""):
            with self.subTest(source=source):
                self.assert_rejected(diff, {"a.txt": source})
        self.assertEqual(run_workflow(diff, scaffold=True)["errors"], [])

    def test_zero_length_target_hunk_checks_insertion_boundary(self):
        diff = "--- a/a.txt\n+++ b/a.txt\n@@ -2 +1,0 @@\n-removed\n"
        result = run_workflow(diff, scaffold=True, file_contents={"a.txt": "kept\n"})
        self.assertEqual(result["errors"], [])
        self.assert_rejected(diff, {"a.txt": ""})

    def test_final_newline_must_match_the_diff(self):
        diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"
        result = run_workflow(diff, scaffold=True, file_contents={"a.txt": "new"})
        self.assertEqual(result["errors"], [])
        self.assert_rejected(diff, {"a.txt": "new\n"})

    def test_missing_sources_still_allow_diff_only_review(self):
        diff = unified("old\n", "new\n", "a.txt") + unified("x\n", "y\n", "b.txt")
        result = run_workflow(diff, scaffold=True, file_contents={"a.txt": "new\n"})
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
