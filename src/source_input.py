"""Single-file snapshots and bounded lookup of user-specified source references."""

from pathlib import Path
import os

from src.config import MAX_DIFF_CHARS
from src.diff import safe_path

MAX_SEARCH_ENTRIES = 10000
_SEARCH_SKIP = {".git", ".venv", "venv", "__pycache__", "output", "generated_tests", "logs", "node_modules", "build", "dist"}


def explicit_reference(task: str, reference: str) -> bool:
    """Require a literal reference, never a suffix of a different requested path."""
    path_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./\\~:-"
    start = task.find(reference)
    while start >= 0:
        end = start+len(reference)
        left_ok = start == 0 or task[start-1] not in path_chars
        right_ok = end == len(task) or task[end] not in path_chars
        if end < len(task) and task[end] == "." and (end+1 == len(task) or task[end+1].isspace()):
            right_ok = True
        if left_ok and right_ok:
            return True
        start = task.find(reference, start+1)
    return False


def read_referenced_target(reference: str, source_root: str | Path, *, run_root=None) -> tuple[Path, str, str]:
    """Resolve an explicit path or a unique basename, without reading other files."""
    root = Path(source_root).resolve(strict=True)
    if "/" in reference or "\\" in reference:
        target = Path(reference).expanduser() if reference.startswith("~/") else reference
        return read_target(target, root, run_root=run_root)
    name = safe_path(reference)
    if name.lower().startswith(".env") or Path(name).suffix.lower() in {".key", ".pem", ".p12"}:
        raise ValueError("不能选择敏感文件作为代码修正目标。")
    matches = []
    visited = 0
    history = Path(run_root).resolve() if run_root is not None else root/"output/runs"
    for current, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs)+len(files)
        if visited > MAX_SEARCH_ENTRIES:
            raise ValueError("项目文件较多，请在任务中提供目标的完整相对路径。")
        dirs[:] = [d for d in dirs if d.lower() not in _SEARCH_SKIP and not d.lower().startswith(".env")
                   and not d.endswith(".egg-info") and not (Path(current)/d).is_symlink()
                   and not (Path(current)/d).resolve().is_relative_to(history)]
        if name in files and not (Path(current)/name).is_symlink():
            matches.append((Path(current)/name).relative_to(root).as_posix())
            if len(matches) > 1:
                raise ValueError("存在多个同名文件，请在任务中提供完整相对路径：" + "、".join(sorted(matches)))
    if not matches:
        raise ValueError("项目内未找到指定文件，请核对文件名或提供完整相对路径。")
    return read_target(matches[0], root, run_root=run_root)


def source_snapshot(path: str, source: str) -> str:
    """Encode full current source as unchanged context, not a fictional new file."""
    path = safe_path(path)
    lines = source.splitlines(keepends=True)
    count = len(lines)
    start = 1 if count else 0
    result = [f"--- a/{path}\n+++ b/{path}\n@@ -{start},{count} +{start},{count} @@\n"]
    for line in lines:
        result.append(" " + line)
        if not line.endswith("\n"):
            result.append("\n\\ No newline at end of file\n")
    return "".join(result)


def read_referenced_targets(references, source_root, *, run_root=None):
    """Resolve explicit files or expand explicit directories within a bounded scope."""
    from src.project_tools import project_inventory
    root = Path(source_root).resolve(strict=True)
    sources = {}
    for reference in references:
        requested = Path(reference).expanduser()
        target = requested if requested.is_absolute() else root/requested
        if target.is_dir():
            relative = target.relative_to(root).as_posix()
            if relative != ".":
                safe_path(relative)
            prefix = "" if relative == "." else relative.rstrip("/")+"/"
            catalog = project_inventory(root, run_root)
            paths = [p for p in catalog if p.startswith(prefix) and Path(p).suffix.lower() in
                     {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h", ".cpp"}]
            if not paths:
                raise ValueError("指定目录中没有可审查的源码文件。")
        else:
            _, path, source = read_referenced_target(reference, root, run_root=run_root)
            paths = [path]
        for path in paths:
            if len(sources) >= 20 and path not in sources:
                raise ValueError("目标超过 20 个文件，请缩小审查目录。")
            _, _, source = read_target(path, root, run_root=run_root)
            sources[path] = source
    return root, sources


def read_target(file_path: str | Path, source_root: str | Path, *, run_root=None) -> tuple[Path, str, str]:
    """Read one explicit UTF-8 target within the root, rejecting sensitive paths."""
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source_root must be a directory")
    requested = Path(file_path)
    if requested.is_absolute():
        # macOS /var and /private/var can name the same authorized root.
        # Keep the relative suffix so links *inside* that root are still rejected.
        aliases = [parent for parent in requested.parents if parent.resolve() == root]
        if not aliases:
            raise ValueError("Target file must stay inside source_root")
        boundary = min(aliases, key=lambda parent: len(parent.parts))
        target = root/requested.relative_to(boundary)
    else:
        target = root/requested
    try:
        relative = safe_path(target.relative_to(root).as_posix())
    except ValueError:
        raise ValueError("Target file must stay inside source_root") from None
    parts = Path(relative).parts
    if (any(part.lower() in {".git", ".venv", "venv", "__pycache__"} or part.lower().startswith(".env") for part in target.parts)
            or target.suffix.lower() in {".key", ".pem", ".p12"}):
        raise ValueError("Target path is sensitive or belongs to project internals")
    current = root
    for part in parts:
        current = current/part
        if current.is_symlink():
            raise ValueError("Target file must not use symbolic links")
    history = Path(run_root).resolve() if run_root is not None else root/"output/runs"
    if target.resolve().is_relative_to(root/"output"):
        raise ValueError("Target file must stay outside generated output")
    if target.resolve().is_relative_to(history):
        raise ValueError("Target file must stay outside the run history")
    if not target.is_file():
        raise ValueError("Target must be an existing source file")
    with target.open("rb") as stream:
        data = stream.read(MAX_DIFF_CHARS*4+1)
    if len(data) > MAX_DIFF_CHARS*4:
        raise ValueError("Target source exceeds the input budget")
    if b"\0" in data:
        raise ValueError("Target must be UTF-8 source text, not binary data")
    try:
        source = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise ValueError("Target must be UTF-8 source text") from None
    return root, relative, source
