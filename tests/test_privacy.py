"""Synthetic markers only; never use real credentials in tests."""

import json
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from examples.repair_demo import CURRENT, DemoModel, unified
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

    def test_auth_headers_and_session_values_are_redacted_in_source_and_mappings(self):
        marker = "unit-" + "x" * 24
        for key in ("Cookie", "Set-Cookie", "Authorization", "Proxy-Authorization", "SESSION_ID", "sessionId", "session"):
            for source in (f'headers = {{"{key}": "{marker}"}}\n', f'{key}: {marker}\r\n', f'{key}={marker}\n'):
                with self.subTest(key=key, form=source[:10]):
                    self.assertNotIn(marker, redact(source))
                    self.assertEqual(len(source.splitlines()), len(redact(source).splitlines()))
            self.assertEqual(to_data({key: marker})[key], "<REDACTED>")
        for value in ("Basic dW5pdDpleGFtcGxl", "Bearer " + marker):
            self.assertNotIn(value.split()[1], redact("Authorization: " + value))

    def test_credentials_never_reach_model_messages_or_run_files(self):
        from pathlib import Path
        import tempfile

        marker = "unit-" + "x" * 24

        class GuardedModel(DemoModel):
            def invoke(self, messages):
                if any(marker in str(message.content) for message in messages):
                    raise AssertionError("Synthetic credential reached the model")
                return super().invoke(messages)

        source = (f'headers = {{"Cookie": "session={marker}", "Authorization": "Basic {marker}"}}\n'
                  f'SESSION_ID = "{marker}"\n' + CURRENT)
        with tempfile.TemporaryDirectory() as directory:
            result = run_workflow(unified("", source, "stats.py", new=True), model=GuardedModel(),
                                  file_contents={"stats.py": source}, run_root=directory)
            self.assertEqual(result["status"], "completed")
            self.assertNotIn(marker, repr(result))
            for path in Path(directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(marker.encode(), path.read_bytes(), str(path))

    def test_redaction_preserves_normal_code_and_is_idempotent(self):
        source = 'session_count = 3\nlabel = "Session history"\n' + CURRENT
        self.assertEqual(redact(source), source)
        secret = 'headers = {"Cookie": "SYNTHETIC_SESSION_VALUE"}\n'
        self.assertEqual(redact(redact(secret)), redact(secret))
        counters = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "session_count": 2}
        self.assertEqual(to_data(counters), counters)


if __name__ == "__main__":
    unittest.main()
