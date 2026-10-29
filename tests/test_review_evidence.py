"""Evidence coordinates and boundaries, without model calls or target execution."""

import unittest

from examples.repair_demo import CURRENT, SAMPLE_DIFF, unified
from src.diff import parse_diff
from src.model_agent import ModelOutputError
from src.review.evidence import evidence_spans, locate_evidence, supplied_evidence
from src.source_input import source_snapshot


class EvidenceTests(unittest.TestCase):
    def test_representative_fixture_corrects_blank_line_references(self):
        # Keep framework evidence tests independent of mutable online inputs.
        source = "def first(value):\n    return value + 1\n\n\ndef second(value):\n    return value * 2\n"
        change = parse_diff(source_snapshot("app/service.py", source))[0]
        for evidence in ("    return value + 1", "    return value * 2"):
            actual = source.splitlines().index(evidence) + 1
            with self.subTest(evidence=evidence):
                self.assertEqual(locate_evidence(change, source, evidence, actual+2), actual)
                self.assertEqual(locate_evidence(change, None, evidence, actual+2), actual)

    def test_missing_or_out_of_range_hint_uses_unique_excerpt(self):
        change = parse_diff(SAMPLE_DIFF)[0]
        for hint in (None, 999):
            self.assertEqual(locate_evidence(change, CURRENT, "return sum(values) / len(values)", hint), 3)

    def test_multiline_excerpt_preserves_valid_hint_inside_span(self):
        change = parse_diff(SAMPLE_DIFF)[0]
        excerpt = CURRENT.strip()
        self.assertEqual(locate_evidence(change, CURRENT, excerpt, 3), 3)
        self.assertEqual(locate_evidence(change, CURRENT, excerpt, 99), 1)

    def test_repeated_evidence_needs_matching_hint(self):
        source = "repeat()\nother()\nrepeat()\n"
        change = parse_diff(unified("", source, "sample.py", new=True))[0]
        self.assertIsNone(locate_evidence(change, source, "repeat()", 2))
        self.assertIsNone(locate_evidence(change, source, "repeat()", None))
        self.assertEqual(locate_evidence(change, source, "repeat()", 3), 3)

    def test_blank_line_inside_multiline_evidence_is_not_an_anchor(self):
        source = "first()\n\nlast()\n"
        change = parse_diff(unified("", source, "sample.py", new=True))[0]
        self.assertEqual(locate_evidence(change, source, source, 2), 1)

    def test_deleted_evidence_has_no_target_line(self):
        change = parse_diff(SAMPLE_DIFF)[0]
        self.assertIsNone(locate_evidence(change, CURRENT, "if not values:", 3))

    def test_old_and_new_lines_cannot_form_one_excerpt(self):
        change = parse_diff(unified("old()\n", "new()\n", "sample.py"))[0]
        with self.assertRaises(ModelOutputError):
            locate_evidence(change, None, "old()\nnew()", 1)

    def test_separate_hunks_cannot_form_one_excerpt(self):
        change = parse_diff("--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-old()\n+first()\n@@ -10 +10 @@\n-old()\n+last()\n")[0]
        with self.assertRaises(ModelOutputError):
            locate_evidence(change, None, "first()\nlast()", 1)

    def test_evidence_cannot_come_from_another_file(self):
        change = parse_diff(SAMPLE_DIFF)[0]
        with self.assertRaises(ModelOutputError):
            locate_evidence(change, CURRENT, "unrelated_file()", 3)

    def test_critical_support_must_exist_in_supplied_context(self):
        changes = parse_diff(SAMPLE_DIFF)
        self.assertFalse(supplied_evidence(changes, {}, None))
        self.assertFalse(supplied_evidence(changes, {}, "public production endpoint"))
        self.assertTrue(supplied_evidence(changes, {}, "return sum(values) / len(values)"))

    def test_crlf_evidence_and_noncontiguous_maps(self):
        self.assertEqual(evidence_spans({1: "first()", 2: "last()"}, "first()\r\nlast()"), [(1, 2)])
        self.assertEqual(evidence_spans({1: "first()", 3: "last()"}, "first()\nlast()"), [])
