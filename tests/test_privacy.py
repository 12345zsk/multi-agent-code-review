"""Synthetic markers only; never use real credentials in tests."""

import json
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from examples.repair_demo import unified
from src.diff import parse_diff
from src.model_agent import invoke_json
from src.privacy import redact
from src.schemas import ReviewReply
from src.serialization import to_data
from src.workflow.service import run_workflow


class PrivacyTests(unittest.TestCase):
    def test_assignments_bearer_and_url_credentials(self):
        marker = "unit-" + "x" * 24
        for source in (f'API_KEY = "{marker}"', f"password='{marker}'", f"DEEPSEEK_API_KEY={marker}", f"+PASSWORD={marker}\n", f"Authorization: Bearer {marker}", f"postgres://user:{marker}@localhost/db"):
            with self.subTest(source_type=source.split(":")[0][:12]):
                self.assertNotIn(marker, redact(source))

    def test_private_key_redaction_preserves_unified_hunk_counts(self):
        marker = "unit-" + "x" * 24
        source = "-----BEGIN PRIVATE KEY-----\n" + marker + "\n-----END PRIVATE KEY-----\n"
        diff = unified("", source, "key.txt", new=True)
        sanitized = redact(diff)
        self.assertNotIn(marker, sanitized)
        self.assertEqual(len(parse_diff(sanitized)[0].hunks[0].lines), 3)

    def test_nested_payload_and_model_output_are_redacted_after_json_decode(self):
        marker = "unit-" + "x" * 24
        model = Mock()
        model.invoke.return_value = AIMessage(content=json.dumps({"summary": f'PASSWORD = "{marker}"', "findings": []}))
        result = invoke_json(model, "test", "Test", {"source": f'API_KEY = "{marker}"', "password": marker}, ReviewReply)
        self.assertNotIn(marker, result.summary)
        self.assertNotIn(marker, str(model.invoke.call_args))
        self.assertEqual(to_data({"authorization": marker})["authorization"], "<REDACTED>")

    def test_scaffold_state_does_not_retain_raw_credentials(self):
        marker = "unit-" + "x" * 24
        source = f'PASSWORD = "{marker}"\n'
        diff = unified("", source, "settings.py", new=True)
        result = run_workflow(diff, scaffold=True, file_contents={"settings.py": source})
        self.assertNotIn(marker, repr(result))


if __name__ == "__main__":
    unittest.main()
