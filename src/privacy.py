"""Best-effort credential redaction before model requests and public output."""

import re

_ASSIGNMENT = re.compile(
    r'''(?im)(\b[\w]*(?:api_?key|secret|password|passwd|token)[\w]*["']?\s*[:=]\s*)'''
    r'''(["'])([^\r\n]*?)\2'''
)
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[A-Z0-9]{16})\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*")
_URL_PASSWORD = re.compile(r"(\w+://[^\s:/@]+:)[^\s/@]+(@)")
_ENV_VALUE = re.compile(
    r"(?im)^([ +\-]?\s*[\w]*(?:api_?key|secret|password|passwd|token)[\w]*\s*=\s*)"
    r"([A-Za-z0-9_./+%=:@-]+)([ \t]*(?:#.*)?)$"
)


def redact(text: str) -> str:
    def private_key(match):
        # Preserve hunk line counts and diff prefixes while removing key material.
        output = []
        for index, line in enumerate(match[0].splitlines(keepends=True)):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            prefix = line[0] if index and line[:1] in {"+", "-", " "} else ""
            output.append(prefix + "<REDACTED PRIVATE KEY>" + ending)
        return "".join(output)

    text = _PRIVATE_KEY.sub(private_key, text)
    text = _ASSIGNMENT.sub(lambda match: match[1] + match[2] + "<REDACTED>" + match[2], text)
    text = _ENV_VALUE.sub(
        lambda match: match[0] if match[2] in {"None", "True", "False", "null", "true", "false", "0"}
        else match[1] + "<REDACTED>" + match[3], text
    )
    text = _TOKEN.sub("<REDACTED>", text)
    text = _BEARER.sub(r"\1<REDACTED>", text)
    return _URL_PASSWORD.sub(r"\1<REDACTED>\2", text)
