"""Command-line review and candidate patch generation; no target code execution."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from src.config import MAX_DIFF_CHARS
from src.diff import parse_diff
from src.privacy import redact
from src.serialization import to_json
from src.workflow.registry import AGENT_IDS


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Git is unavailable or timed out") from None
    if result.returncode:
        raise ValueError("Git command failed; check the repository and revision")
    return result.stdout


def _revision(ref: str) -> str:
    value = _run_git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ValueError("Invalid Git revision")
    return value


def _get_diff_and_files(args: argparse.Namespace) -> tuple[str, list[str]]:
    options = ("--no-ext-diff", "--no-textconv", "--no-color")
    if args.file:
        path = Path(args.file)
        if path.stat().st_size > MAX_DIFF_CHARS * 4:
            raise ValueError("Diff file exceeds the input budget")
        diff = path.read_text(encoding="utf-8")
    elif args.branch:
        diff = _run_git("diff", *options, f"{_revision(args.branch)}...HEAD", "--")
    elif args.commit:
        diff = _run_git("show", "--format=", "--root", *options, _revision(args.commit), "--")
    elif args.unstaged:
        diff = _run_git("diff", *options, "--")
    else:
        diff = _run_git("diff", *options, "--cached", "--")
    if not diff.strip():
        return "", []
    return diff, [change.path for change in parse_diff(diff)]


def _read_sources(paths: list[str], root: str | None) -> dict[str, str]:
    if root is None:
        return {}
    base = Path(root).resolve(strict=True)
    if not base.is_dir():
        raise ValueError("--source-root must be a directory")
    sources = {}
    total = 0
    for relative in paths:
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Source path escapes --source-root")
        if path.name.startswith(".env") or path.suffix in {".key", ".pem", ".p12"}:
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_DIFF_CHARS * 3:
            raise ValueError("Source file exceeds the context budget")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        total += len(content)
        if total > MAX_DIFF_CHARS * 3:
            raise ValueError("Source context exceeds the input budget")
        sources[relative] = content
    return sources


def _write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print("Saved: " + redact(str(path.resolve())), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="code-review", description="Multi-agent review and candidate repair with built-in ReAct.")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--branch", metavar="REF", help="Review changes from REF to HEAD")
    inputs.add_argument("--commit", metavar="SHA", help="Review one commit, including a root commit")
    inputs.add_argument("--unstaged", action="store_true", help="Review unstaged changes (default: staged)")
    inputs.add_argument("--file", metavar="PATH", help="Read a unified diff file")
    inputs.add_argument("--resume", metavar="RUN_ID", help="Resume a failed/interrupted persisted run")
    inputs.add_argument("--inspect", metavar="RUN_ID", help="Inspect a saved run and its diagnostics without model calls")
    parser.add_argument("--run-root", default="output/runs", help="Private run history and checkpoints (default: output/runs)")
    parser.add_argument("--allow-code-change", action="store_true", help="Explicitly allow resuming after workflow code changes")
    parser.add_argument("--additional-fix-attempts", type=int, default=1, help="Additional attempts when resuming a rejected repair")
    parser.add_argument("--finding", help="Filter --inspect events by finding ID")
    parser.add_argument("--node", help="Filter --inspect by workflow node/agent")
    parser.add_argument("--phase", help="Filter --inspect by execution phase")
    parser.add_argument("--task", help="Natural-language review/repair goal; unsupported or unclear requests stop before execution")
    parser.add_argument("--mode", choices=("review", "fix"), help="Execution mode (default without --task: review)")
    parser.add_argument("--agents", nargs="+", choices=AGENT_IDS, help="Specialists to run (default: all eight)")
    parser.add_argument("--max-fix-attempts", type=int, default=2)
    parser.add_argument("--source-root", help="Explicitly read changed CURRENT source files under this directory")
    parser.add_argument("--write-files", action="store_true", help="Write validated fix candidates under --source-root, preserving backups")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", metavar="PATH", help="Report path (default: ./output/review.md or fix-review.md; .json with --format json)")
    parser.add_argument("--patch-output", metavar="PATH", help="Code patch path (default in fix mode: ./output/repair.diff); does not apply it")
    parser.add_argument("--test-output", metavar="PATH", help="Test patch path (default in fix mode: ./output/tests.diff); does not apply it")
    parser.add_argument("--scaffold", action="store_true", help="Explicit offline skeleton mode, with no model calls")
    parser.add_argument("--graph", action="store_true", help="Print the Mermaid graph without a model or diff")
    args = parser.parse_args()
    try:
        if args.max_fix_attempts < 1:
            raise ValueError("--max-fix-attempts must be positive")
        if args.task is not None and (args.graph or args.inspect or args.resume or args.scaffold):
            raise ValueError("--task requires a new model workflow; cannot combine with graph/inspect/resume/scaffold")
        if args.write_files and ((args.mode != "fix" and args.task is None) or not args.source_root or args.scaffold or args.graph or args.inspect or args.resume):
            raise ValueError("--write-files requires --mode fix and --source-root, without scaffold/graph/inspect/resume")
        if args.inspect:
            from src.runtime.store import RunStore
            store = RunStore.open(args.run_root, args.inspect)
            events = [event for event in store.read_events() if (not args.agents or event["node"] in args.agents)
                      and (not args.finding or args.finding in event.get("finding_ids", []))
                      and (not args.node or event["node"] == args.node) and (not args.phase or event.get("stage") == args.phase)]
            diagnostics_path = store.path/"diagnostics.json"
            diagnostics = json.loads(diagnostics_path.read_text()) if diagnostics_path.exists() else []
            print(redact(json.dumps({"manifest": store.metadata, "diagnostics": diagnostics, "events": events}, ensure_ascii=False, indent=2)))
            return
        if args.finding or args.node or args.phase:
            raise ValueError("--finding, --node and --phase require --inspect")
        if args.resume:
            if args.file or args.source_root or args.agents or args.scaffold:
                raise ValueError("Resume uses saved inputs, sources and agent selection")
            from src.workflow.service import resume_workflow
            result = resume_workflow(args.resume, run_root=args.run_root, allow_code_change=args.allow_code_change,
                                     additional_fix_attempts=args.additional_fix_attempts)
            args.mode = result["mode"]
            text = to_json(result) if args.format == "json" else result["final_review"]
        elif args.graph:
            from src.workflow.graph import build_workflow_graph
            text = build_workflow_graph().get_graph().draw_mermaid()
            result = None
        else:
            diff, paths = _get_diff_and_files(args)
            if not diff:
                if args.task is not None:
                    raise ValueError("自然语言任务需要待审查的 diff；请使用 --file 或提供 Git 变更。未执行任何任务。")
                print("No diff found. Nothing to review.", file=sys.stderr)
                return
            from src.workflow.service import run_workflow
            result = run_workflow(
                diff, paths, mode=args.mode, active_agents=args.agents, task=args.task,
                max_fix_attempts=args.max_fix_attempts, scaffold=args.scaffold,
                file_contents=_read_sources(paths, args.source_root),
                run_root=args.run_root,
                write_root=args.source_root if args.write_files else None,
            )
            args.mode = result["mode"]
            text = to_json(result) if args.format == "json" else result["final_review"]
        if result is None:
            if args.output:
                _write_output(Path(args.output), text + "\n")
            else:
                print(text)
            return
        extension = "json" if args.format == "json" else "md"
        report_name = "fix-review" if args.mode == "fix" else "review"
        report_path = Path(args.output) if args.output else Path("output") / f"{report_name}.{extension}"
        from src.runtime.store import RunStore, publish_run
        store = RunStore.open(args.run_root, result["run_id"])
        latest = publish_run(store, report_path=report_path,
                             patch_path=args.patch_output or Path("output")/"repair.diff",
                             test_path=args.test_output or Path("output")/"tests.diff", report_format=args.format)
        print("Run ID: " + result["run_id"], file=sys.stderr)
        print("Saved: " + redact(str(report_path.resolve())), file=sys.stderr)
        print("Run directory: " + redact(str(store.path)), file=sys.stderr)
        for item in result.get("write_result", {}).get("files", []):
            if item["status"] in {"written", "already_written"}:
                print("Written: " + redact(str(Path(result["write_root"])/item["file_path"])), file=sys.stderr)
        if not args.output:
            print(text)
        for name in ("repair.diff", "tests.diff"):
            if name in latest["exports"]:
                print("Saved: " + redact(latest["exports"][name]["path"]), file=sys.stderr)
        if result["status"] in {"unsupported", "needs_clarification"}:
            raise SystemExit(2)
        if not args.scaffold and result["status"] in {"failed", "incomplete"}:
            if any("RUN_INTERRUPTED" in error for error in result.get("errors", [])):
                raise SystemExit(130)
            if any("MODEL_CONFIGURATION_INVALID" in error for error in result.get("errors", [])):
                print("code-review: check DEEPSEEK_API_KEY and model configuration", file=sys.stderr)
                raise SystemExit(2)
            raise SystemExit(1)
    except (ValueError, OSError, UnicodeError) as exc:
        # Only plain application ValueErrors have controlled, credential-free text.
        message = redact(str(exc)) if type(exc) is ValueError else type(exc).__name__
        print("code-review: " + message, file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"code-review: request failed ({type(exc).__name__}); raw diagnostics suppressed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
