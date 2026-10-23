"""Locate exact evidence without joining old/new code or disconnected hunks."""

from src.diff import FileChange
from src.model_agent import ModelOutputError


def evidence_spans(lines: dict[int, str], evidence: str) -> list[tuple[int, int]]:
    """Return matching line spans in contiguous supplied lines only."""
    needle = "\n".join(evidence.strip().splitlines())
    if not needle:
        return []
    runs = []
    for number in sorted(lines):
        if not runs or number != runs[-1][-1] + 1:
            runs.append([])
        runs[-1].append(number)
    spans = set()
    for run in runs:
        text = "\n".join(lines[number] for number in run)
        offset = text.find(needle)
        while offset >= 0:
            start = run[0] + text[:offset].count("\n")
            spans.add((start, start + needle.count("\n")))
            offset = text.find(needle, offset + 1)
    return sorted(spans)


def old_lines(change: FileChange) -> dict[int, str]:
    lines = {}
    for hunk in change.hunks:
        number = hunk.old_start
        for kind, content in hunk.lines:
            if kind != "+":
                lines[number] = content.rstrip("\r\n")
                number += 1
    return lines


def locate_evidence(change: FileChange, source: str | None, evidence: str, hint: int | None) -> int | None:
    lines = dict(enumerate(source.splitlines(), 1)) if source is not None else change.target_lines()
    spans = evidence_spans(lines, evidence)
    if not spans:
        if evidence_spans(old_lines(change), evidence):
            # Removed code has no target-side location; never point to unrelated new code.
            return None
        raise ModelOutputError("Finding evidence is not present in the supplied file",
                               code="FINDING_EVIDENCE_MISSING", stage="finding_evidence")
    if len(spans) == 1:
        start, end = spans[0]
        return hint if hint is not None and start <= hint <= end and lines[hint].strip() else start
    if hint is not None and lines.get(hint, "").strip() and any(start <= hint <= end for start, end in spans):
        return hint
    # Repeated excerpts do not justify guessing an occurrence.
    return None


def supplied_evidence(changes: list[FileChange], sources: dict[str, str], evidence: str | None) -> bool:
    if not evidence:
        return False
    maps = [dict(enumerate(source.splitlines(), 1)) for source in sources.values()]
    maps.extend(lines for change in changes for lines in (change.target_lines(), old_lines(change)))
    return any(evidence_spans(lines, evidence) for lines in maps)
