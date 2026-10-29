"""Bounded project discovery and private, versioned source observations."""

import ast
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.diff import parse_diff, safe_path, validate_source_context
from src.privacy import is_sensitive_path, redact
from src.source_input import read_target, _SEARCH_SKIP

MAX_PROJECT_ENTRIES = 2000
MAX_CONTEXT_CHARS = 120000
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h", ".cpp",
                 ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".md", ".rst", ".txt"}


def eligible(path):
    parts = Path(path).parts
    return (not is_sensitive_path(path)
            and not any(part.startswith(".") or part.lower() in _SEARCH_SKIP for part in parts)
            and Path(path).suffix.lower() in TEXT_SUFFIXES)


def project_inventory(root, run_root=None, *, include_all_files=False):
    if not root:
        return {}
    root = Path(root).resolve(strict=True)
    history = Path(run_root).resolve() if run_root else root/"output"
    result, visited = {}, 0
    for current, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > MAX_PROJECT_ENTRIES:
            raise ValueError("Project discovery budget exceeded; use a narrower source_root")
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in _SEARCH_SKIP
                         and not d.endswith(".egg-info") and not (Path(current)/d).is_symlink()
                         and not (Path(current)/d).resolve().is_relative_to(history))
        for name in sorted(files):
            path = Path(current)/name
            relative = path.relative_to(root).as_posix()
            permitted = (not is_sensitive_path(relative) and not any(part.startswith(".") for part in Path(relative).parts)
                         if include_all_files else eligible(relative))
            if len(relative) > 500 or not permitted or path.is_symlink() or path.resolve().is_relative_to(history):
                continue
            stat = path.stat()
            if include_all_files or stat.st_size <= 160000:
                result[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return result


def available_sources(state):
    result = dict(state.get("file_contents", {}))
    for sources in state.get("observed_sources", {}).values():
        for path, source in sources.items():
            if path in result and result[path] != source:
                raise ValueError("Conflicting source snapshots")
            result[path] = source
    if sum(map(len, result.values())) > MAX_CONTEXT_CHARS:
        raise ValueError("Combined source context exceeds the budget")
    return result


class ProjectContext:
    def __init__(self, sources, *, root=None, inventory=None, run_dir=None, review_diff=None):
        self.sources = dict(sources)
        self.root = Path(root).resolve(strict=True) if root else None
        self.inventory = dict(inventory or {})
        self.run_dir = Path(run_dir) if run_dir else None
        self.target_changes = {change.path: change for change in parse_diff(review_diff)} if review_diff else {}

    def read(self, path):
        path = safe_path(path)
        if is_sensitive_path(path) or any(part.startswith(".") for part in Path(path).parts):
            raise ValueError("SOURCE_NOT_AVAILABLE")
        if path in self.sources:
            return self.sources[path]
        if self.root is None or path not in self.inventory or not eligible(path):
            raise ValueError("SOURCE_NOT_AVAILABLE")
        from src.runtime.store import file_lock, write_json
        cache = self.run_dir/"context_snapshots" if self.run_dir else None
        record = cache/(hashlib.sha256(path.encode()).hexdigest()+".json") if cache else None
        with file_lock(cache/".lock", blocking=True) if cache else nullcontext():
            if record and record.exists():
                saved = json.loads(record.read_text())
                source = saved["source"]
                if saved["path"] != path or saved["sha256"] != hashlib.sha256(source.encode()).hexdigest():
                    raise ValueError("SOURCE_SNAPSHOT_INVALID")
            else:
                target = self.root/path
                before = target.stat()
                if {"size": before.st_size, "mtime_ns": before.st_mtime_ns} != self.inventory[path]:
                    raise ValueError("SOURCE_VERSION_CHANGED")
                _, _, source = read_target(path, self.root, run_root=self.run_dir.parent if self.run_dir else None)
                after = target.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("SOURCE_VERSION_CHANGED")
                source = redact(source)
            if sum(map(len, self.sources.values())) + len(source) > MAX_CONTEXT_CHARS:
                raise ValueError("CONTEXT_BUDGET_EXCEEDED")
            if path in self.target_changes:
                try:
                    validate_source_context({path: source}, [self.target_changes[path]])
                except ValueError:
                    raise ValueError("SOURCE_DIFF_MISMATCH") from None
            if record and not record.exists():
                write_json(record, {"path": path, "source": source, "sha256": hashlib.sha256(source.encode()).hexdigest()})
            self.sources[path] = source
            return source


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadSourceArgs(ToolArgs):
    file_path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 <= self.end_line-self.start_line < 200:
            raise ValueError("Read at most 200 lines")
        return self


class ListArgs(ToolArgs):
    directory: str = Field(default=".", max_length=500)
    limit: int = Field(default=100, ge=1, le=100)


class SearchArgs(ListArgs):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


class AnalyzeArgs(ToolArgs):
    file_path: str = Field(min_length=1, max_length=500)


def make_project_tools(context):
    def paths(directory):
        prefix = "" if directory == "." else safe_path(directory).rstrip("/")+"/"
        return sorted(p for p in context.sources.keys() | context.inventory.keys() if p.startswith(prefix) and eligible(p))

    @tool(args_schema=ReadSourceArgs)
    def read_source(file_path: str, start_line: int = 1, end_line: int = 200) -> dict:
        """Read numbered lines from an input snapshot or an authorized project file; snapshot on first access."""
        file_path = safe_path(file_path)
        try:
            source = context.read(file_path)
        except ValueError as exc:
            code = str(exc)
            return {"status": "error", "code": code if code in {"SOURCE_NOT_AVAILABLE", "SOURCE_VERSION_CHANGED", "SOURCE_SNAPSHOT_INVALID", "SOURCE_DIFF_MISMATCH", "CONTEXT_BUDGET_EXCEEDED"} else "SOURCE_NOT_AVAILABLE"}
        except OSError:
            return {"status": "error", "code": "SOURCE_NOT_AVAILABLE"}
        lines = source.splitlines()
        if start_line > max(1, len(lines)):
            return {"status": "error", "code": "LINE_RANGE_INVALID"}
        selected = [{"line": i, "text": s} for i, s in enumerate(lines[start_line-1:end_line], start_line)]
        if sum(len(item["text"]) for item in selected) > 16000:
            return {"status": "error", "code": "TOOL_OUTPUT_TOO_LARGE"}
        return {"status": "ok", "file_path": file_path, "total_lines": len(lines), "lines": selected,
                "source_hash": hashlib.sha256(source.encode()).hexdigest()}

    @tool(args_schema=ListArgs)
    def list_project_files(directory: str = ".", limit: int = 100) -> dict:
        """List bounded, non-sensitive project paths; never traverse outside the authorized source root."""
        found = paths(directory)
        return {"status": "ok", "files": found[:limit], "truncated": len(found) > limit}

    @tool(args_schema=SearchArgs)
    def search_code(query: str, directory: str = ".", limit: int = 20) -> dict:
        """Search a literal string in up to 20 authorized text files; return bounded numbered matches."""
        found, skipped = [], []
        catalog = paths(directory)
        for path in catalog[:20]:
            try:
                source = context.read(path)
            except (ValueError, OSError):
                skipped.append(path)
                continue
            for line, text in enumerate(source.splitlines(), 1):
                if query in text:
                    found.append({"file_path": path, "line": line, "text": text[:300]})
                    if len(found) == limit:
                        return {"status": "ok", "matches": found, "truncated": True, "unavailable": skipped}
        return {"status": "ok", "matches": found, "truncated": len(catalog) > 20, "unavailable": skipped}

    @tool(args_schema=AnalyzeArgs)
    def analyze_python(file_path: str) -> dict:
        """Parse Python imports and definitions without importing or executing the target module."""
        if Path(file_path).suffix.lower() != ".py":
            return {"status": "error", "code": "NOT_PYTHON_SOURCE"}
        try:
            tree = ast.parse(context.read(file_path))
        except SyntaxError as exc:
            return {"status": "ok", "syntax_valid": False, "line": exc.lineno}
        except (ValueError, OSError):
            return {"status": "error", "code": "SOURCE_NOT_AVAILABLE_OR_CHANGED"}
        symbols = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append({"kind": type(node).__name__, "name": node.name, "line": node.lineno})
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                symbols.append({"kind": "import", "module": getattr(node, "module", None),
                                "names": [a.name for a in node.names], "line": node.lineno})
            if len(symbols) == 100:
                break
        return {"status": "ok", "syntax_valid": True, "symbols": symbols, "truncated": len(symbols) == 100}

    return {t.name: t for t in (read_source, list_project_files, search_code, analyze_python)}


def react_options(state, observed, *, tool_observations=None):
    options = {"sources": available_sources(state), "source_root": state.get("source_root"),
            "inventory": state.get("project_inventory", {}), "run_dir": state.get("run_dir"),
            "observed_sources": observed, "review_diff": state.get("code_diff")}
    if tool_observations is not None:
        options["tool_observations"] = tool_observations
    return options
