"""Evaluate saved structured results or live cases against human-authored labels."""

import argparse
import json
from pathlib import Path
import sys

from evals.scoring import aggregate, annotation_template, score_report
from src.runtime.store import new_run_id, write_json


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="Saved run result.json; no model calls")
    parser.add_argument("--labels", help="Human-authored expected findings JSON")
    parser.add_argument("--annotations", help="Completed human mapping and repair assessments JSON")
    parser.add_argument("--write-annotation-template", metavar="PATH", help="Create a run-bound template for human review")
    parser.add_argument("--mode", choices=("review", "fix"), default="review")
    parser.add_argument("--output", default="output/evaluation.json")
    parser.add_argument("--run-root", default="output/evaluations")
    args = parser.parse_args()
    try:
        if args.report:
            report_path = Path(args.report)
            report = json.loads(report_path.read_text())
            if args.write_annotation_template:
                destination = Path(args.write_annotation_template)
                if destination.resolve() == report_path.resolve() or destination.exists():
                    raise ValueError("Annotation template must use a new path distinct from the result")
                write_json(destination, annotation_template(report))
                print("Saved annotation template: " + str(destination.resolve()))
                return
            if not args.labels:
                raise ValueError("--labels is required when evaluating a saved report")
            annotations = json.loads(Path(args.annotations).read_text()) if args.annotations else None
            results = [score_report(report, json.loads(Path(args.labels).read_text()), annotations)]
        else:
            if args.labels or args.annotations or args.write_annotation_template:
                raise ValueError("Labels and manual annotations require --report")
            from evals.cases import CASES
            from src.workflow.service import run_workflow
            from src.diff import apply_patch, parse_diff
            root = Path(args.run_root)/new_run_id()/"runs"
            results = []
            for case in CASES:
                changes = parse_diff(case["diff"])
                sources = apply_patch({path: "" for path in case["file_paths"]}, changes)
                state = run_workflow(case["diff"], case["file_paths"], mode=args.mode, file_contents=sources, run_root=root)
                from src.serialization import to_json
                report = json.loads(to_json(state))
                result = score_report(report, case["labels"])
                results.append(result)
                print(case["name"]+": "+state["status"]+" | Run ID: "+state["run_id"])
        summary = {"schema_version": 1, "cases": results, "summary": aggregate(results)}
        destination = Path(args.output)
        protected = [Path(value).resolve() for value in (args.report, args.labels, args.annotations) if value]
        if destination.resolve() in protected:
            raise ValueError("Evaluation output must not overwrite its inputs")
        write_json(destination, summary)
        print(json.dumps(summary["summary"], ensure_ascii=False, indent=2))
        print("Saved: "+str(destination.resolve()))
        counts = summary["summary"]
        if counts["failed_cases"] or counts["metrics"]["false_positives"] or counts["metrics"]["false_negatives"]:
            raise SystemExit(1)
    except (ValueError, OSError, KeyError) as exc:
        print("Evaluation failed ("+type(exc).__name__+"); check report, label and annotation schemas.", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    run()
