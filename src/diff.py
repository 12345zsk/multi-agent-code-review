"""Parse and apply unified text diffs in memory. No subprocess or file writes."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import codecs
import re


class DiffError(ValueError):
    pass


def safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path or any(ord(char) < 32 for char in path):
        raise DiffError("Invalid relative path")
    value = PurePosixPath(path)
    if str(value) == "." or value.is_absolute() or any(part in {"..", ".git"} for part in value.parts) or ":" in path:
        raise DiffError("Path must stay inside the project")
    return str(value)


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FileChange:
    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    def target_lines(self) -> dict[int, str]:
        result = {}
        for hunk in self.hunks:
            line = hunk.new_start
            for kind, content in hunk.lines:
                if kind != "-":
                    result[line] = content.rstrip("\r\n")
                    line += 1
        return result


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")


def _header_path(header: str) -> str | None:
    raw = header[4:].rstrip("\r\n").split("\t", 1)[0]
    if raw.startswith('"'):
        if not raw.endswith('"'):
            raise DiffError("Invalid quoted path")
        try:
            raw = codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8")
        except (ValueError, UnicodeError):
            raise DiffError("Invalid quoted path encoding") from None
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return safe_path(raw)


def parse_diff(text: str) -> list[FileChange]:
    lines = text.splitlines(keepends=True)
    if any(line.startswith(("GIT binary patch", "Binary files ", "diff --cc ", "diff --combined ", "rename from ")) for line in lines):
        raise DiffError("Binary, rename and combined diffs are unsupported; review text changes separately")
    changes = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.startswith("--- "):
            if raw.startswith(("@@", "+", "-", " ")) and raw.strip():
                raise DiffError("Unexpected diff body")
            index += 1
            continue
        old_path = _header_path(raw)
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise DiffError("Missing target file header")
        new_path = _header_path(lines[index])
        if old_path is None and new_path is None:
            raise DiffError("A file change requires a path")
        index += 1
        hunks = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK.fullmatch(lines[index].rstrip("\r\n"))
            if not match:
                raise DiffError("Invalid hunk header")
            start_old, count_old, start_new, count_new = match.groups()
            old_count = int(count_old) if count_old is not None else 1
            new_count = int(count_new) if count_new is not None else 1
            if (old_count and int(start_old) < 1) or (new_count and int(start_new) < 1):
                raise DiffError("Nonempty hunk ranges must start at line one or later")
            body = []
            old_seen = new_seen = 0
            index += 1
            while old_seen < old_count or new_seen < new_count:
                if index >= len(lines) or lines[index][:1] not in {" ", "+", "-", "\\"}:
                    raise DiffError("Incomplete hunk")
                entry = lines[index]
                index += 1
                if entry.startswith("\\ No newline at end of file"):
                    if not body:
                        raise DiffError("Invalid newline marker")
                    body[-1] = (body[-1][0], body[-1][1].rstrip("\r\n"))
                    continue
                kind, content = entry[0], entry[1:]
                if kind == "\\":
                    raise DiffError("Invalid hunk marker")
                old_seen += kind != "+"
                new_seen += kind != "-"
                if old_seen > old_count or new_seen > new_count:
                    raise DiffError("Hunk line counts do not match")
                body.append((kind, content))
            if index < len(lines) and lines[index].startswith("\\ No newline at end of file"):
                if not body:
                    raise DiffError("Invalid newline marker")
                body[-1] = (body[-1][0], body[-1][1].rstrip("\r\n"))
                index += 1
            hunks.append(Hunk(int(start_old), old_count, int(start_new), new_count, tuple(body)))
        changes.append(FileChange(old_path, new_path, tuple(hunks)))
    if not changes:
        raise DiffError("No unified text diff found; binary and combined diffs are unsupported")
    paths = [change.path for change in changes]
    if len(paths) != len(set(paths)):
        raise DiffError("Duplicate file sections")
    return changes


def apply_patch(contents: dict[str, str], changes: list[FileChange]) -> dict[str, str]:
    result = dict(contents)
    for change in changes:
        if change.old_path and change.new_path and change.old_path != change.new_path:
            raise DiffError("Rename patches are not supported for automatic proposals")
        if change.old_path is None:
            if change.new_path in result:
                raise DiffError("New file already exists")
            original = []
        elif change.old_path not in result:
            raise KeyError(change.old_path)
        else:
            original = result[change.old_path].splitlines(keepends=True)
        output = []
        cursor = 0
        if not change.hunks:
            raise DiffError("Patch has no text hunks")
        for hunk in change.hunks:
            start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
            if start < cursor or start > len(original):
                raise DiffError("Hunk position outside baseline")
            output.extend(original[cursor:start])
            cursor = start
            target = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
            if len(output) != target:
                raise DiffError("Target hunk position does not match")
            for kind, content in hunk.lines:
                if kind != "+":
                    if cursor >= len(original) or original[cursor] != content:
                        raise DiffError("Patch context does not match baseline")
                    cursor += 1
                if kind != "-":
                    output.append(content)
        output.extend(original[cursor:])
        if change.new_path is None:
            if output:
                raise DiffError("Deletion leaves file contents")
            result.pop(change.old_path, None)
        else:
            result[change.new_path] = "".join(output)
    return result


def validate_source_context(contents: dict[str, str], changes: list[FileChange]) -> None:
    """Check supplied post-change sources against the diff's visible target side.

    Reverse application checks exact content, coordinates and newline markers.
    Missing sources remain valid for diff-only reviews; unseen regions of an
    existing file cannot be authenticated from a partial diff.
    """
    for change in changes:
        message = f"Source context does not match the diff target for {change.path}; provide matching post-change sources"
        if change.new_path is None:
            if change.old_path in contents:
                raise ValueError(message)
            continue
        if change.new_path not in contents:
            continue
        reverse = FileChange(change.new_path, change.old_path, tuple(
            Hunk(hunk.new_start, hunk.new_count, hunk.old_start, hunk.old_count,
                 tuple(({"+": "-", "-": "+", " ": " "}[kind], content) for kind, content in hunk.lines))
            for hunk in change.hunks
        ))
        try:
            apply_patch({change.new_path: contents[change.new_path]}, [reverse])
        except DiffError:
            raise ValueError(message) from None
