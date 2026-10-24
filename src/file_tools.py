"""Write approved repair candidates with version guards, backups and receipts."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from src.diff import safe_path
from src.privacy import redact
from src.runtime.store import write_json


def _hash(data):
    return hashlib.sha256(data).hexdigest()


class FileWriteError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code, self.stage = code, "file_write"


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=240000)


def _replace(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="."+path.name+"-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class CandidateWriter:
    """The caller supplies the exact candidates that passed repair validation."""

    def __init__(self, root, *, sources, candidates, run_dir):
        self.root = Path(root).resolve(strict=True)
        self.run_dir = Path(run_dir).resolve(strict=True)
        self.directory = self.run_dir/"writes"
        self.sources, self.candidates = dict(sources), dict(candidates)
        self.tool = StructuredTool.from_function(
            self.write_file, name="write_file", args_schema=WriteFileArgs,
            description="Write an approved repair candidate to its authorized local path, preserving a backup.",
        )

    def _prepare(self, file_path, content):
        try:
            relative = safe_path(file_path)
        except ValueError:
            raise FileWriteError("WRITE_PATH_INVALID") from None
        if relative not in self.candidates or content != self.candidates[relative]:
            raise FileWriteError("WRITE_CANDIDATE_MISMATCH")
        parts = Path(relative).parts
        if any(part.lower() == ".git" or part.lower().startswith(".env") for part in parts) or Path(relative).suffix.lower() in {".key", ".pem", ".p12"}:
            raise FileWriteError("WRITE_PATH_FORBIDDEN")
        target = self.root
        for part in parts:
            target = target/part
            if target.is_symlink():
                raise FileWriteError("WRITE_PATH_INVALID")
        if not target.resolve().is_relative_to(self.root) or target.resolve().is_relative_to(self.run_dir.parent):
            raise FileWriteError("WRITE_PATH_INVALID")
        if target.exists() and not target.is_file():
            raise FileWriteError("WRITE_PATH_INVALID")
        baseline = self.sources.get(relative)
        if "<REDACTED" in content or redact(content) != content:
            raise FileWriteError("WRITE_CONTENT_REJECTED")
        original = target.read_bytes() if target.exists() else None
        candidate = content.encode("utf-8")
        # CLI text inputs normalize newlines; preserve an existing CRLF file.
        if original is not None and b"\r\n" in original and original.count(b"\r\n") == original.count(b"\n") and "\r" not in content:
            candidate = content.replace("\n", "\r\n").encode("utf-8")
        key = _hash(relative.encode())
        receipt_path, backup_path = self.directory/(key+".json"), self.directory/(key+".bak")
        record = {"file_path": relative, "baseline_hash": _hash(baseline.encode()) if baseline is not None else None,
                  "candidate_hash": _hash(content.encode()), "written_hash": _hash(candidate),
                  "backup": "writes/"+key+".bak" if baseline is not None else None}
        already_written = False
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            if any(previous.get(name) != value for name, value in record.items()):
                raise FileWriteError("WRITE_RECEIPT_MISMATCH")
            if baseline is not None:
                if not backup_path.is_file() or _hash(backup_path.read_bytes()) != previous.get("original_hash"):
                    raise FileWriteError("WRITE_BACKUP_INVALID")
            record["original_hash"] = previous.get("original_hash")
            already_written = original is not None and _hash(original) == record["written_hash"]
            if not already_written and previous.get("status") == "written":
                raise FileWriteError("FILE_VERSION_CHANGED")
        if not already_written:
            if baseline is None and original is not None:
                raise FileWriteError("WRITE_NEW_FILE_EXISTS")
            try:
                current = original.decode("utf-8") if original is not None else None
            except UnicodeError:
                raise FileWriteError("FILE_VERSION_CHANGED") from None
            if current != baseline and (current is None or current.replace("\r\n", "\n").replace("\r", "\n") != baseline):
                raise FileWriteError("FILE_VERSION_CHANGED")
            record["original_hash"] = _hash(original) if original is not None else None
            if backup_path.exists() and (original is None or backup_path.read_bytes() != original):
                raise FileWriteError("WRITE_BACKUP_INVALID")
        return target, candidate, original, receipt_path, backup_path, record, already_written

    def preflight(self):
        """Check all files before the first write; each write also rechecks."""
        from src.runtime.tracing import trace_stage
        for path, content in self.candidates.items():
            with trace_stage("file_write_preflight", file_path=path):
                self._prepare(path, content)

    def write_file(self, file_path: str, content: str) -> dict:
        target, candidate, original, receipt, backup, record, done = self._prepare(file_path, content)
        if not done:
            if original is not None and not backup.exists():
                _replace(backup, original)
            record["status"] = "prepared"
            write_json(receipt, record)
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
            _replace(target, candidate, mode)
        record["status"] = "written"
        write_json(receipt, record)
        return {"file_path": record["file_path"], "status": "already_written" if done else "written",
                "sha256": record["written_hash"], "backup": record["backup"]}


def read_write_result(root, run_dir, candidates):
    """Report actual progress after an interrupted multi-file write."""
    root, directory = Path(root).resolve(), Path(run_dir)/"writes"
    files = []
    try:
        for relative in sorted(candidates):
            receipt = directory/(_hash(relative.encode())+".json")
            if not receipt.exists():
                continue
            record = json.loads(receipt.read_text())
            target = root/safe_path(relative)
            if not target.resolve().is_relative_to(root) or target.is_symlink():
                raise FileWriteError("WRITE_PATH_INVALID")
            done = target.is_file() and _hash(target.read_bytes()) == record["written_hash"]
            files.append({"file_path": relative, "status": "written" if done else "pending_or_changed",
                          "sha256": record["written_hash"], "backup": record.get("backup")})
    except (ValueError, KeyError, OSError):
        return {"status": "unknown", "files": files}
    status = "written" if files and len(files) == len(candidates) and all(item["status"] == "written" for item in files) else "partial" if files else "not_run"
    return {"status": status, "files": files, "backup_dir": str(directory)}
