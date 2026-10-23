"""JSON-safe, redacted workflow artifacts."""

from dataclasses import asdict, is_dataclass
import json
import re

from src.privacy import redact


def to_data(value):
    if is_dataclass(value):
        return to_data(asdict(value))
    if isinstance(value, dict):
        return {
            key: "<REDACTED>" if re.fullmatch(
                r"(?i)(?:[a-z0-9]+_)*(?:api_key|secret|password|passwd|access_token|auth_token|token|cookie|authorization)(?:_[a-z0-9]+)*", str(key)
            ) else to_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_data(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def to_json(state: dict) -> str:
    # Raw inputs and full candidate source are not part of exported reports.
    return json.dumps(to_data({
        key: value for key, value in state.items()
        if key not in {"code_diff", "file_contents", "context", "candidate_files", "previous_attempt", "user_task"}
    }), ensure_ascii=False, indent=2)
