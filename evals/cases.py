"""Two review scopes of one multi-file project, with independent human labels."""

import hashlib
import json
from pathlib import Path

from src.diff import parse_diff, validate_source_context
from src.source_input import source_snapshot


def load_cases():
    repository = Path(__file__).resolve().parents[1]
    root = repository/"examples/order_project"
    sources = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
               for path in sorted((root/"app").glob("*.py"))}
    diff = (repository/"examples/order_project.diff").read_text(encoding="utf-8")
    changed = {change.path for change in parse_diff(diff)}
    cases = []
    for name, scope, code_diff, contents in (
        ("order_project", "file", "".join(source_snapshot(p, s) for p, s in sources.items()), sources),
        ("order_project_diff", "diff", diff, {p: sources[p] for p in sources if p in changed}),
    ):
        labels = json.loads((repository/"evals/labels"/(name+".json")).read_text(encoding="utf-8"))
        if any(hashlib.sha256(contents[path].encode()).hexdigest() != value
               for path, value in labels["source_hashes"].items()):
            raise ValueError("Online sample changed; restore its original version before baseline evaluation")
        validate_source_context(contents, parse_diff(code_diff))
        cases.append({"name": name, "review_scope": scope, "source_root": root,
                      "file_paths": list(contents), "diff": code_diff, "sources": contents, "labels": labels})
    return cases
