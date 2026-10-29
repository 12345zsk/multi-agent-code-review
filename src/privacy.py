"""Best-effort credential redaction before model requests and public output."""

from pathlib import PurePosixPath
import re

_SECRET_NAME = (
    r"(?:[\w]*(?:api_?key|secret|password|passwd|token)[\w]*|"
    r"(?:[\w]+_)?(?:cookies?|set-cookie|(?:proxy-)?authorization|session(?:_?id)?))"
)

_ASSIGNMENT = re.compile(
    rf'''(?im)(\b{_SECRET_NAME}["']?\s*[:=]\s*)'''
    r'''(["'])([^\r\n]*?)\2'''
)
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[A-Z0-9]{16})\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_BEARER = re.compile(r"(?i)(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/-]+=*")
_AUTH_HEADER = re.compile(r"(?im)^([ +\-]?[ \t]*(?:cookies?|set-cookie|(?:proxy-)?authorization)\s*:[ \t]*)[^\r\n]+")
_URL_PASSWORD = re.compile(r"(\w+://[^\s:/@]+:)[^\s/@]+(@)")
_ENV_VALUE = re.compile(
    rf"(?im)^([ +\-]?[ \t]*{_SECRET_NAME}[ \t]*[:=][ \t]*)"
    r"([A-Za-z0-9_./+%=:@-]+)([ \t]*(?:#[^\r\n]*)?\r?)$"
)


def is_sensitive_key(key):
    # Keep usage counters such as input_tokens intact; source-text matching has
    # broader variable-name rules than structured credential fields.
    return re.fullmatch(
        r"(?:[a-z0-9]+_)*(?:api_key|secret|password|passwd|access_token|auth_token|token|"
        r"cookies?|set-cookie|(?:proxy-)?authorization)(?:_[a-z0-9]+)*|"
        r"(?:[a-z0-9]+_)*(?:session_?id|session)", str(key), re.IGNORECASE,
    ) is not None


def is_sensitive_path(path):
    """Shared credential-path exclusion for local readers and writers."""
    value = PurePosixPath(str(path).lower())
    return (any(part in {".git", ".venv", "venv", "__pycache__", ".ssh", ".aws", ".azure", ".kube",
                         ".gnupg", ".docker", ".npmrc", ".netrc", ".pypirc",
                         "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
                or part.startswith(".env") for part in value.parts)
            or value.suffix in {".key", ".pem", ".p12", ".pfx", ".ppk", ".jks", ".keystore"})


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
    text = _AUTH_HEADER.sub(lambda match: match[1] + "<REDACTED>", text)
    text = _ENV_VALUE.sub(
        lambda match: match[0] if match[2] in {"None", "True", "False", "null", "true", "false", "0"}
        else match[1] + "<REDACTED>" + match[3], text
    )
    text = _TOKEN.sub("<REDACTED>", text)
    text = _BEARER.sub(r"\1<REDACTED>", text)
    return _URL_PASSWORD.sub(r"\1<REDACTED>\2", text)
