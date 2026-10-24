"""Generate candidate patches and validate them in memory, without running code."""

import ast
from dataclasses import asdict, replace
from pathlib import PurePosixPath

from src.contracts import FixPlan, PatchArtifact, ValidationResult, WorkflowState
from src.diff import DiffError, apply_patch, parse_diff
from src.model_agent import ModelOutputError, invoke_json
from src.repair.patches import canonicalize_patch, scope_new_tests
from src.runtime.tracing import digest, trace_stage
from src.schemas import FailureDiagnosisReply, PatchReply, PlanReply, RegressionReply, TestReply
from src.project_tools import available_sources, react_options
from src.task_intent import task_context


def _repair_input(state: WorkflowState) -> dict:
    return {
        **task_context(state),
        "file_paths": state["file_paths"],
        "diff": state["code_diff"], "sources": available_sources(state),
        "findings": [asdict(f) for f in state["findings"] if f.status == "confirmed" and f.fixable],
        "previous_attempt": state.get("previous_attempt", {}),
    }


def fix_planner_node(state: WorkflowState, model) -> dict:
    reply = invoke_json(
        model, "fix_planner",
        "Plan minimal changes for all supplied confirmed fixable findings. Reference every supplied ID exactly once. "
        "Use the supplied current sources as the repair baseline; do not revert unrelated changes. "
        "Incorporate any previous static-validation or regression feedback. Do not claim checks were executed.",
        _repair_input(state), PlanReply,
    )
    expected = {f.id for f in state["findings"] if f.status == "confirmed" and f.fixable}
    if set(reply.finding_ids) != expected or len(reply.finding_ids) != len(expected):
        raise ModelOutputError("Repair plan must cover every fixable finding exactly once", code="PLAN_COVERAGE_INVALID", stage="plan_validation")
    return {"fix_plan": FixPlan("ready", tuple(reply.finding_ids), reply.description)}


def patch_generator_node(state: WorkflowState, model) -> dict:
    reply = invoke_json(
        model, "patch_generator",
        "Generate a minimal unified code diff against the supplied current source baseline. "
        "Use --- a/path and +++ b/path headers and exact @@ line counts, with LF newlines. "
        "Include \\ No newline at end of file after any old or new line that lacks a final newline. "
        "Modify only files in the reviewed scope; do not generate tests in this step. "
        "In file scope, the context-only input diff is a snapshot of an existing file; do not recreate it as a new file. "
        "Cover exactly the plan finding IDs. Do not embed credentials or unrelated refactoring. "
        "Return the complete candidate patch for this attempt, not an incremental patch over a previous attempt.",
        {**_repair_input(state), "plan": asdict(state["fix_plan"])}, PatchReply,
    )
    if set(reply.finding_ids) != set(state["fix_plan"].finding_ids) or len(reply.finding_ids) != len(set(reply.finding_ids)):
        raise ModelOutputError("Patch finding IDs do not match the plan", code="PATCH_COVERAGE_INVALID", stage="patch_validation")
    return {"patch": PatchArtifact("generated", reply.diff, tuple(reply.finding_ids), reply.summary)}


def test_generator_node(state: WorkflowState, model) -> dict:
    reply = invoke_json(
        model, "test_generator",
        "Generate focused regression tests for the candidate code patch. Return a valid unified diff. "
        "Only modify test files; new test files use --- /dev/null. For Python use standard-library unittest only, never pytest or new third-party test dependencies. "
        "Place all newly generated tests directly in generated_tests/, separate from framework tests/. "
        "Existing test files may be edited only if explicitly included in file_paths; observed context is read-only. "
        "Use test_*.py filenames and define unittest.TestCase subclasses with test_ methods, runnable with python -m unittest discover -s generated_tests -v. "
        "When new_test_suffix is supplied, append it to each new Python test filename stem before .py. "
        "Keep new test modules self-contained; do not import other newly generated test modules. "
        "Tests must assert the desired contract, including the reported failure and a normal case. "
        "If meaningful tests cannot be generated from supplied context, return not_needed with an explicit limitation and empty diff. "
        "Never claim tests were executed or passed.",
        {**_repair_input(state), "patch": asdict(state["patch"]),
         "new_test_suffix": "__run_" + digest(state["run_id"])[:12] if state.get("write_root") and state.get("run_id") else None}, TestReply,
    )
    return {"test_patch": PatchArtifact(reply.status, reply.diff, state["fix_plan"].finding_ids, reply.summary)}


def _is_test_path(path: str) -> bool:
    file = PurePosixPath(path)
    return (
        any(part in {"test", "tests", "__tests__", "generated_tests"} for part in file.parts[:-1])
        or file.name.startswith("test_") or file.name.endswith("_test.py")
        or ".test." in file.name or ".spec." in file.name
    )


def _check_unittest(source: str) -> None:
    tree = ast.parse(source)
    modules, bases = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest" or alias.name.startswith("pytest."):
                    raise DiffError("Generated Python tests must use standard-library unittest, not pytest")
                if alias.name == "unittest":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "pytest" or node.module.startswith("pytest.")):
                raise DiffError("Generated Python tests must use standard-library unittest, not pytest")
            if node.module == "unittest":
                bases.update(alias.asname or alias.name for alias in node.names if alias.name == "TestCase")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(base, ast.Name) and base.id in bases)
            or (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                and base.value.id in modules and base.attr == "TestCase")
            for base in node.bases
        ) and any(isinstance(method, ast.FunctionDef) and method.name.startswith("test_") for method in node.body):
            return
    raise DiffError("Generated Python tests need a unittest.TestCase with test_ methods")


def validator_node(state: WorkflowState) -> dict:
    checks = []
    artifacts = {}
    stage, file_path = "artifact_presence", ""
    try:
        patch = state["patch"]
        test_patch = state["test_patch"]
        if not patch or not test_patch or test_patch.status not in {"generated", "not_needed"}:
            raise DiffError("Missing code or test artifact")
        if "<REDACTED" in patch.diff or "<REDACTED" in test_patch.diff:
            raise DiffError("A redacted patch requires manual reconstruction")
        baseline = available_sources(state)
        stage = "code_patch_normalization"
        with trace_stage(stage):
            patch = replace(patch, diff=canonicalize_patch(patch.diff, baseline))
        stage = "test_patch_normalization"
        with trace_stage(stage):
            test_patch = replace(test_patch, diff=canonicalize_patch(test_patch.diff, baseline)) if test_patch.status == "generated" else test_patch
        artifacts = {"patch": patch, "test_patch": test_patch}
        checks.append("canonical_diff")
        code_changes = parse_diff(patch.diff)
        test_changes = parse_diff(test_patch.diff) if test_patch.status == "generated" else []
        checks.append("unified_diff")
        stage = "path_scope"
        allowed = set(state["file_paths"])
        if any(change.path not in allowed or (change.old_path and change.new_path and change.old_path != change.new_path) for change in code_changes):
            raise DiffError("Code patch changed an unreviewed path or renamed a file")
        if any(not _is_test_path(change.path) for change in test_changes):
            raise DiffError("Test patch changed a non-test file")
        for change in test_changes:
            if change.old_path is not None and change.path not in allowed:
                file_path = change.path
                raise DiffError("Existing test edits require explicit target scope; related context is read-only")
        if any(change.path.endswith(".py") and not PurePosixPath(change.path).name.startswith("test_") for change in test_changes):
            raise DiffError("Generated Python test filenames must match test_*.py for unittest discovery")
        if {change.path for change in code_changes} & {change.path for change in test_changes}:
            raise DiffError("Code and test patches overlap")
        checks.append("path_scope")
        stage = "finding_references"
        if set(patch.finding_ids) != set(state["fix_plan"].finding_ids):
            raise DiffError("Patch references do not match plan")
        checks.append("finding_references")
        if test_patch.status == "generated":
            stage = "test_path_allocation"
            with trace_stage(stage):
                suffix = "__run_" + digest(state["run_id"])[:12] if state.get("write_root") and state.get("run_id") else ""
                scoped = scope_new_tests(test_patch.diff, suffix)
            if scoped != test_patch.diff:
                test_patch = replace(test_patch, diff=scoped,
                                     summary=test_patch.summary + " New test files are stored separately in generated_tests/.")
                artifacts["test_patch"] = test_patch
                test_changes = parse_diff(scoped)
            checks.append("generated_test_paths")
            if suffix:
                checks.append("run_scoped_test_paths")
        missing = [change.old_path for change in [*code_changes, *test_changes] if change.old_path and change.old_path not in baseline]
        if missing:
            return {
                **artifacts,
                "validation": ValidationResult(
                    "not_run", tuple(checks),
                    "Patch structure checked; complete post-change source is missing for application and syntax validation.",
                ),
                "candidate_files": {},
            }
        stage = "patch_application"
        with trace_stage(stage):
            candidate = apply_patch(baseline, code_changes)
        if all(candidate.get(change.path) == baseline.get(change.path) for change in code_changes):
            raise DiffError("Code patch does not change the baseline")
        candidate = apply_patch(candidate, test_changes) if test_changes else candidate
        checks.append("in_memory_application")
        changed_paths = {change.path for change in [*code_changes, *test_changes]}
        for path in sorted(changed_paths):
            if path.endswith(".py") and path in candidate:
                stage, file_path = "python_syntax", path
                with trace_stage(stage, file_path=path):
                    compile(candidate[path], path, "exec", dont_inherit=True)
                checks.append("python_syntax:" + path)
                if any(change.path == path for change in test_changes):
                    stage = "unittest_discovery"
                    _check_unittest(candidate[path])
                    checks.append("unittest_discovery:" + path)
        return {
            **artifacts,
            "candidate_files": {path: candidate[path] for path in changed_paths if path in candidate},
            "validation": ValidationResult(
                "passed", tuple(checks), "Static checks passed in memory. No code, imports, tests or shell commands were executed."
            ),
        }
    except (DiffError, SyntaxError, KeyError, ValueError) as exc:
        # Do not include SyntaxError.text or raw source/credential-bearing output.
        reason = str(exc) if isinstance(exc, DiffError) else type(exc).__name__
        return {
            **artifacts,
            "validation": ValidationResult("failed", tuple(checks), "Static validation failed: " + reason,
                                           code="PYTHON_SYNTAX_INVALID" if isinstance(exc, SyntaxError) else "PATCH_VALIDATION_FAILED",
                                           stage=stage, file_path=file_path, line=exc.lineno if isinstance(exc, SyntaxError) else None),
            "candidate_files": {},
        }


def regression_reviewer_node(state: WorkflowState, model) -> dict:
    reply = invoke_json(
        model, "regression_reviewer",
        "Review the proposed code and test diffs against the original findings and baseline. "
        "Return passed only when the supplied evidence supports resolution without a new defect. "
        "Return failed for remaining or introduced issues, not_run for insufficient context. "
        "This is a model review, never proof of runtime tests. Do not claim anything was executed.",
        {**_repair_input(state), "patch": asdict(state["patch"]),
         "test_patch": asdict(state["test_patch"]), "static_validation": asdict(state["validation"]),
         "candidate_sources": state.get("candidate_files", {})}, RegressionReply,
    )
    return {"regression": ValidationResult(reply.status, ("model_review",), reply.summary, scope="model")}


def failure_diagnoser_node(state: WorkflowState, model) -> dict:
    """Classify failed validation and collect missing evidence before a bounded repair retry."""
    from src.react import invoke_react_json
    observed = {}
    reply = invoke_react_json(model, "failure_diagnoser",
        "Diagnose this failed repair from structured validation/regression feedback, not guesses. "
        "Distinguish code patch, test patch, missing context, regression, and unsupported work. "
        "Use project tools to collect missing caller/configuration/contract evidence when needed. "
        "action=collect_context requires actually collecting new source evidence. Give concrete repair "
        "instructions; do not blindly repeat the previous plan. Choose stop if evidence cannot justify "
        "a safe correction, or the attempt budget is exhausted. Never expand target/write scope or execute code.",
        {**_repair_input(state), "patch": asdict(state["patch"]) if state.get("patch") else None,
         "test_patch": asdict(state["test_patch"]) if state.get("test_patch") else None,
         "validation": asdict(state.get("validation", ValidationResult())),
         "regression": asdict(state.get("regression", ValidationResult())),
         "remaining_attempts": max(0, state["max_fix_attempts"]-state["fix_attempts"])},
        FailureDiagnosisReply, **react_options(state, observed))
    diagnosis = reply.model_dump()
    if reply.action == "collect_context" and not set(observed)-set(available_sources(state)):
        diagnosis.update(action="stop", summary="No additional context was obtained. " + reply.summary)
    if reply.category == "unsupported" or state["fix_attempts"] >= state["max_fix_attempts"]:
        diagnosis["action"] = "stop"
    diagnosis["attempt"] = state["fix_attempts"]
    return {"failure_diagnosis": diagnosis, "failure_diagnoses": [*state.get("failure_diagnoses", []), diagnosis],
            "observed_sources": {"failure_diagnoser": observed}}


def file_writer_node(state: WorkflowState) -> dict:
    """Invoke write_file only for complete, validated repair candidates."""
    from pathlib import Path
    from src.file_tools import CandidateWriter, FileWriteError
    from src.runtime.store import file_lock
    from src.runtime.tracing import digest, trace_detail

    if (state.get("mode") != "fix" or not state.get("write_root") or not state.get("run_dir")
            or state.get("errors") or state.get("pending_steps")
            or state.get("regression", ValidationResult()).status != "passed"):
        raise FileWriteError("WRITE_NOT_AUTHORIZED")
    checked = validator_node(state)
    if checked["validation"].status != "passed" or checked.get("candidate_files") != state.get("candidate_files"):
        raise FileWriteError("WRITE_VALIDATION_REQUIRED")
    for key in ("patch", "test_patch"):
        artifact = state.get(key)
        if artifact and artifact.diff and any(change.new_path is None for change in parse_diff(artifact.diff)):
            raise FileWriteError("WRITE_DELETE_UNSUPPORTED")
    writer = CandidateWriter(state["write_root"], sources=available_sources(state),
                             candidates=state["candidate_files"], run_dir=state["run_dir"])
    results = []
    with file_lock(Path(state["run_dir"]).parent/".write.lock", blocking=True):
        from src.privacy import redact
        from src.source_input import read_target
        for path, expected in available_sources(state).items():
            if path in state.get("project_inventory", {}) and path not in state["candidate_files"]:
                with trace_stage("context_version_check", file_path=path):
                    try:
                        _, _, current = read_target(path, state["write_root"], run_root=Path(state["run_dir"]).parent)
                    except (ValueError, OSError):
                        raise FileWriteError("CONTEXT_VERSION_CHANGED") from None
                    if redact(current) != expected:
                        raise FileWriteError("CONTEXT_VERSION_CHANGED")
        with trace_stage("file_write_preflight"):
            writer.preflight()
        for path, content in sorted(state["candidate_files"].items()):
            metadata = {"tool": "write_file", "tool_call_id": digest([state["run_id"], state["fix_attempts"], path])[:24],
                        "file_path": path}
            with trace_stage("tool_execution", **metadata):
                trace_detail("tool_started", **metadata, arguments_hash=digest({"file_path": path, "content": content}))
                result = writer.tool.invoke({"file_path": path, "content": content})
                trace_detail("tool_completed", **metadata, status=result["status"],
                             result_hash=digest(result), written_hash=result["sha256"], backup=result["backup"])
            results.append(result)
    return {"write_result": {"status": "written", "files": results, "backup_dir": str(writer.directory)}}
