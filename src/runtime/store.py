"""Atomic, per-run local storage; checkpoints are private and never use pickle."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from src.serialization import to_data, to_json
from src.runtime.tracing import digest, timestamp


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="."+path.name+"-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(to_data(value), ensure_ascii=False, indent=2)+"\n")


@contextmanager
def file_lock(path, *, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise ValueError("Run is already active; concurrent resume is not allowed") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def code_fingerprint():
    base = Path(__file__).resolve().parents[1]
    return digest({str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(base.rglob("*.py"))})


def new_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid4().hex[:12]


class RunStore:
    def __init__(self, root, run_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", run_id):
            raise ValueError("Invalid run ID")
        self.path = Path(root).resolve()/run_id
        self.run_id = run_id

    @classmethod
    def create(cls, root, inputs):
        store = cls(root, new_run_id())
        store.path.mkdir(parents=True, mode=0o700)
        store.metadata = {
            "schema_version": 1, "run_id": store.run_id, "status": "running",
            "started_at": timestamp(), "code_hash": code_fingerprint(), "input_hash": digest(inputs),
            "mode": inputs["mode"], "scaffold": inputs.get("scaffold", False), "resumes": 0,
            "artifacts": {},
        }
        write_json(store.path/"input.json", inputs)
        store.save_metadata()
        return store

    @classmethod
    def open(cls, root, run_id):
        store = cls(root, run_id)
        if not store.path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError("Run path escapes the run history directory")
        store.metadata = json.loads((store.path/"manifest.json").read_text())
        if store.metadata.get("schema_version") != 1 or store.metadata.get("run_id") != run_id:
            raise ValueError("Run manifest does not match the supported schema and run ID")
        return store

    def save_metadata(self):
        write_json(self.path/"manifest.json", self.metadata)

    def read_inputs(self):
        inputs = json.loads((self.path/"input.json").read_text())
        if digest(inputs) != self.metadata["input_hash"]:
            raise ValueError("Saved run input integrity check failed")
        return inputs

    def append_event(self, item):
        with (self.path/"events.jsonl").open("a", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(json.dumps(item, ensure_ascii=False)+"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def read_events(self, *, repair=False):
        path = self.path/"events.jsonl"
        if not path.exists():
            return []
        # A killed process can leave a partial final line; committed events remain readable.
        raw = path.read_text()
        lines = raw.splitlines()
        events = []
        for index, line in enumerate(lines):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                if index != len(lines)-1:
                    raise ValueError("Run event log is corrupt") from None
                if repair:
                    atomic_write(path, "".join(json.dumps(event)+"\n" for event in events))
        if repair and raw and not raw.endswith("\n"):
            atomic_write(path, "".join(json.dumps(event)+"\n" for event in events))
        return events

    def save_drafts(self, state, *, stage="final"):
        records = []
        for key, filename in (("patch", "repair.diff"), ("test_patch", "tests.diff")):
            artifact = state.get(key)
            if artifact and artifact.diff:
                relative = Path("drafts")/getattr(self, "session_id", "initial")/f"attempt-{state.get('fix_attempts', 0)}"/stage/filename
                atomic_write(self.path/relative, artifact.diff)
                records.append({"path": str(relative), "sha256": hashlib.sha256((self.path/relative).read_bytes()).hexdigest(),
                                "finding_ids": list(artifact.finding_ids)})
        return records

    def finish(self, state):
        self.save_drafts(state)
        report = "fix-review.md" if state["mode"] == "fix" else "review.md"
        atomic_write(self.path/report, state["final_review"]+"\n")
        atomic_write(self.path/"result.json", to_json(state)+"\n")
        write_json(self.path/"diagnostics.json", state.get("diagnostics", []))
        eligible = (state.get("status") == "proposed" and state.get("validation")
                    and state["validation"].status == "passed" and state.get("regression")
                    and state["regression"].status == "passed")
        artifacts = {}
        for filename, classification in ((report, "report"), ("result.json", "structured_result"),
                                          ("events.jsonl", "trace"), ("diagnostics.json", "diagnostics")):
            path = self.path/filename
            if path.exists():
                artifacts[filename] = {"path": filename, "classification": classification,
                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        candidate_dir = self.path/"candidates"
        for name in ("repair.diff", "tests.diff"):
            (candidate_dir/name).unlink(missing_ok=True)
        if eligible:
            for key, name in (("patch", "repair.diff"), ("test_patch", "tests.diff")):
                artifact = state.get(key)
                if artifact and artifact.diff:
                    atomic_write(candidate_dir/name, artifact.diff)
                    artifacts[name] = {"path": "candidates/"+name, "classification": "validated_candidate",
                                       "sha256": hashlib.sha256((candidate_dir/name).read_bytes()).hexdigest()}
        for path in sorted((self.path/"drafts").rglob("*.diff")):
            relative = str(path.relative_to(self.path))
            artifacts[relative] = {"path": relative, "classification": "draft",
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted((self.path/"writes").glob("*")):
            if path.is_file() and path.suffix in {".bak", ".json"}:
                relative = str(path.relative_to(self.path))
                artifacts[relative] = {"path": relative, "classification": "source_backup" if path.suffix == ".bak" else "write_receipt",
                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        self.metadata.update(status=state["status"], mode=state["mode"], finished_at=timestamp(), model_name=state.get("model_name", "unknown"),
                             artifacts=artifacts, candidate_valid=bool(eligible),
                             write_status=state.get("write_result", {}).get("status", "not_run"),
                             write_root=state.get("write_root"),
                             write_paths=sorted(set(state.get("file_paths", [])) | set(state.get("candidate_files", {}))),
                             checkpoint_available=(self.path/"checkpoint.sqlite").exists())
        self.save_metadata()


def publish_run(store, *, report_path, patch_path, test_path, output_root="output", report_format="markdown"):
    """The latest manifest is the commit marker for the compatibility outputs."""
    targets = [Path(report_path).resolve(), Path(patch_path).resolve(), Path(test_path).resolve()]
    if len(set(targets)) != 3:
        raise ValueError("Report, code patch and test patch output paths must be distinct")
    if any(path.is_relative_to(store.path.parent) for path in targets):
        raise ValueError("Compatibility outputs must stay outside the private run history")
    root = Path(output_root)
    with file_lock(store.path/".run.lock"), file_lock(root/".publish.lock", blocking=True):
        current = json.loads((store.path/"manifest.json").read_text())
        if current.get("write_root"):
            protected = {(Path(current["write_root"])/path).resolve() for path in current.get("write_paths", [])}
            if set(targets) & protected:
                raise ValueError("Output paths must not overwrite repair targets; inspect run " + store.run_id)
        if (current.get("completion_id") != store.metadata.get("completion_id")
                or current.get("session_id") != current.get("completion_id")):
            raise ValueError("Run changed before publication; inspect the latest run manifest")
        report_name = "result.json" if report_format == "json" else "fix-review.md" if store.metadata["mode"] == "fix" else "review.md"
        buffers = {}
        for filename in (report_name, "repair.diff", "tests.diff"):
            artifact = store.metadata["artifacts"].get(filename)
            if not artifact:
                continue
            path = (store.path/artifact["path"]).resolve()
            if not path.is_relative_to(store.path.resolve()):
                raise ValueError("Artifact path escapes the run directory")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                raise ValueError("Artifact integrity check failed")
            if filename.endswith(".diff") and (not store.metadata["candidate_valid"] or artifact["classification"] != "validated_candidate"):
                raise ValueError("A draft cannot be published as a validated candidate")
            buffers[filename] = content.decode("utf-8")
        _archive_legacy(root)
        atomic_write(targets[0], buffers[report_name])
        exports = {"report": {"path": str(targets[0]), "sha256": store.metadata["artifacts"][report_name]["sha256"], "run_id": store.run_id}}
        for target, filename in zip(targets[1:], ("repair.diff", "tests.diff")):
            if filename in buffers:
                atomic_write(target, buffers[filename])
                exports[filename] = {"path": str(target), "sha256": store.metadata["artifacts"][filename]["sha256"], "run_id": store.run_id}
            else:
                target.unlink(missing_ok=True)
        latest = {"schema_version": 1, "run_id": store.run_id, "mode": store.metadata["mode"],
                  "status": store.metadata["status"], "run_dir": str(store.path),
                  "write_status": store.metadata.get("write_status", "not_run"),
                  "candidate_valid": store.metadata["candidate_valid"], "exports": exports}
        write_json(root/("latest-"+store.metadata["mode"]+".json"), {key: latest[key] for key in ("run_id", "mode", "status", "run_dir", "candidate_valid")})
        write_json(root/"latest.json", latest)
    return latest


def _archive_legacy(root):
    if (root/"latest.json").exists():
        return
    old = [root/name for name in ("review.md", "fix-review.md", "review.json", "fix-review.json", "repair.diff", "tests.diff") if (root/name).is_file()]
    if not old:
        return
    destination = root/"legacy"/new_run_id()
    destination.mkdir(parents=True, mode=0o700)
    artifacts = {}
    for path in old:
        atomic_write(destination/path.name, path.read_text())
        artifacts[path.name] = {"classification": "legacy_untracked", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    write_json(destination/"manifest.json", {"schema_version": 1, "ownership": "unknown_legacy_run", "archived_at": timestamp(), "artifacts": artifacts})
