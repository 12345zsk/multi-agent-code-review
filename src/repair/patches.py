"""Canonicalize model diffs using exact supplied context, never fuzzy matching."""

from dataclasses import replace
import difflib
import re

from src.diff import DiffError, apply_patch, parse_diff

_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")


def _recount(text: str) -> str:
    lines = text.splitlines(keepends=True)
    result = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@"):
            result.append(line)
            index += 1
            continue
        match = _HEADER.fullmatch(line.rstrip("\r\n"))
        if not match:
            raise DiffError("Invalid model hunk header")
        old_start, _, new_start, _ = match.groups()
        index += 1
        body = []
        while index < len(lines) and lines[index][:1] in {" ", "+", "-", "\\"}:
            if lines[index].startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
                break
            body.append(lines[index])
            index += 1
        old_count = sum(line[:1] in {" ", "-"} for line in body)
        new_count = sum(line[:1] in {" ", "+"} for line in body)
        result.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n")
        result.extend(body)
    return "".join(result)


def _matches_source(original: list[str], old: list[str], start: int) -> bool:
    if start < 0 or start + len(old) > len(original):
        return False
    actual = original[start:start + len(old)]
    if actual == old:
        return True
    # Infer only a missing marker on the actual final source line. Every other
    # character and line ending must match, including all interior lines.
    return bool(actual and start + len(old) == len(original)
                and not actual[-1].endswith(("\n", "\r"))
                and actual[:-1] == old[:-1] and old[-1] == actual[-1] + "\n")


def canonicalize_patch(text: str, sources: dict[str, str]) -> str:
    """Recount hunks and reanchor unambiguous source matches, recovering EOF markers.

    The public diff parser remains strict. Missing source permits count repair
    only; the validator must still report that application was not checked.
    Only the supplied source can establish a missing old-side EOF marker;
    added lines retain the newlines expressed by the model patch.
    """
    changes = parse_diff(_recount(text))
    output = []
    for change in changes:
        if change.old_path is not None and change.old_path not in sources:
            # Recounting alone does not establish that an unavailable source matches.
            section = f"--- a/{change.old_path}\n+++ {'b/' + change.new_path if change.new_path else '/dev/null'}\n"
            for hunk in change.hunks:
                section += f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@\n"
                section += _body_text(hunk.lines)
            output.append(section)
            continue
        baseline = sources.get(change.old_path, "")
        original = baseline.splitlines(keepends=True)
        cursor = delta = 0
        hunks = []
        for hunk in change.hunks:
            old = [content for kind, content in hunk.lines if kind != "+"]
            start = hunk.old_start - 1 if old else hunk.old_start
            if old and not (cursor <= start and _matches_source(original, old, start)):
                matches = [index for index in range(cursor, len(original) - len(old) + 1)
                           if _matches_source(original, old, index)]
                if len(matches) != 1:
                    raise DiffError("Model patch context is missing or ambiguous in the supplied source")
                start = matches[0]
            if start < cursor or start > len(original):
                raise DiffError("Model patch insertion is outside the supplied source")
            source_lines = iter(original[start:start + len(old)])
            hunks.append(replace(
                hunk, old_start=start + (1 if hunk.old_count else 0),
                new_start=start + delta + (1 if hunk.new_count else 0),
                lines=tuple((kind, content if kind == "+" else next(source_lines))
                            for kind, content in hunk.lines),
            ))
            cursor = start + len(old)
            delta += hunk.new_count - hunk.old_count
        anchored = replace(change, hunks=tuple(hunks))
        updated = apply_patch({change.old_path: baseline} if change.old_path else {}, [anchored])
        generated = difflib.unified_diff(
            original, updated.get(change.new_path, "").splitlines(keepends=True),
            fromfile="a/" + change.old_path if change.old_path else "/dev/null",
            tofile="b/" + change.new_path if change.new_path else "/dev/null",
        )
        for line in generated:
            output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    canonical = "".join(output)
    if not canonical:
        raise DiffError("Model patch does not change the baseline")
    parse_diff(canonical)
    return canonical


def _body_text(lines: tuple[tuple[str, str], ...]) -> str:
    return "".join(kind + content if content.endswith("\n") else kind + content + "\n\\ No newline at end of file\n"
                   for kind, content in lines)
