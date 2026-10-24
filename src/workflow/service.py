"""Review/repair API with optional durable execution and safe, structured tracing."""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from importlib.metadata import version
from pathlib import Path
import hashlib
import os
import sqlite3
from typing import Literal

from src.contracts import AgentReport, NodeHandler, ValidationResult, WorkflowState
from src.config import MAX_DIFF_CHARS
from src.diff import parse_diff, safe_path, validate_source_context
from src.privacy import redact
from src.runtime.store import RunStore, code_fingerprint, new_run_id
from src.runtime.tracing import NodeExecutionError, TraceRecorder, digest, locations
from src.workflow.graph import build_workflow_graph
from src.workflow.nodes import reporter_node
from src.workflow.registry import AGENT_IDS


def run_workflow(
    code_diff: str, file_paths: list[str] | None = None, *,
    mode: Literal["review", "fix"] | None = None, active_agents: Sequence[str] | None = None,
    max_fix_attempts: int = 2, handlers: Mapping[str, NodeHandler] | None = None,
    model=None, file_contents: Mapping[str, str] | None = None, scaffold: bool = False,
    run_root: str | Path | None = None,
    write_root: str | Path | None = None,
    auto_write_root: str | Path | None = None,
    source_root: str | Path | None = None,
    task: str | None = None,
    review_scope: Literal["diff", "file"] = "diff",
    target_file: str | None = None,
) -> WorkflowState:
    """Review with ReAct; task admission may enable guarded writes within auto_write_root."""
    if not isinstance(code_diff, str) or not code_diff.strip():
        raise ValueError("code_diff must not be empty")
    if len(code_diff) > MAX_DIFF_CHARS:
        raise ValueError("Diff exceeds MAX_DIFF_CHARS; split it into smaller reviews")
    if mode not in (None, "review", "fix"):
        raise ValueError("mode must be review or fix")
    if review_scope not in ("diff", "file"):
        raise ValueError("review_scope must be diff or file")
    if target_file is not None and (review_scope != "file" or not isinstance(target_file, str) or not Path(target_file).is_absolute()):
        raise ValueError("target_file requires an absolute path in file scope")
    if task is not None:
        from src.task_intent import MAX_TASK_CHARS
        if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
            raise ValueError("task must contain 1 to 4000 characters")
        if scaffold:
            raise ValueError("Natural-language tasks require a model; scaffold cannot interpret them")
    if type(max_fix_attempts) is not int or max_fix_attempts < 1:
        raise ValueError("max_fix_attempts must be a positive integer")
    if write_root is not None:
        if (mode != "fix" and task is None) or scaffold or run_root is None:
            raise ValueError("write_root requires fix mode, a persistent run_root and no scaffold")
        write_root = Path(write_root).resolve(strict=True)
        if not write_root.is_dir():
            raise ValueError("write_root must be a directory")
    if auto_write_root is not None:
        if task is None or scaffold:
            raise ValueError("auto_write_root requires a natural-language task and no scaffold")
        auto_write_root = Path(auto_write_root).resolve(strict=True)
        if not auto_write_root.is_dir():
            raise ValueError("auto_write_root must be a directory")
        if write_root is not None and auto_write_root != write_root:
            raise ValueError("Automatic and explicit write boundaries must match")
    if active_agents is not None:
        raise ValueError("Expert dispatch is dynamic; active_agents is no longer supported")
    project_root = source_root or write_root or auto_write_root
    if project_root is not None:
        project_root = Path(project_root).resolve(strict=True)
        if not project_root.is_dir() or any(boundary and Path(boundary) != project_root for boundary in (write_root, auto_write_root)):
            raise ValueError("Source and write boundaries must match")
    sanitized_diff = redact(code_diff)
    changes = parse_diff(sanitized_diff)
    paths = [change.path for change in changes]
    if file_paths is not None and set(map(safe_path, file_paths)) != set(paths):
        raise ValueError("file_paths must match the files in code_diff")
    sources = {safe_path(path): redact(content) for path, content in (file_contents or {}).items()}
    if sum(len(content) for content in sources.values()) > MAX_DIFF_CHARS*3:
        raise ValueError("Source context exceeds the input budget")
    validate_source_context(sources, changes)
    _validate_file_scope(sanitized_diff, paths, sources, review_scope)
    build_workflow_graph(handlers, model=model, scaffold=scaffold)
    inputs = {"workflow_version": 3, "code_diff": sanitized_diff, "file_paths": paths, "file_contents": sources,
              "mode": mode or "review", "active_agents": [], "max_fix_attempts": max_fix_attempts, "scaffold": scaffold, "react": not scaffold,
              "user_task": redact(task) if task is not None else "", "requested_mode": mode,
              "source_root": str(project_root) if project_root else None,
              "run_history_root": str(Path(run_root).resolve()) if run_root else None,
              "review_scope": review_scope, "target_file": target_file,
              "write_root": str(write_root) if write_root is not None else None,
              "auto_write_root": str(auto_write_root) if auto_write_root is not None else None}
    store = RunStore.create(run_root, inputs) if run_root is not None else None
    return _execute(inputs, store, model=model, handlers=handlers)


def _validate_file_scope(code_diff, paths, sources, review_scope):
    if review_scope == "file":
        from src.source_input import source_snapshot
        if not paths or set(sources) != set(paths) or code_diff != "".join(source_snapshot(path, sources[path]) for path in paths):
            raise ValueError("File scope requires complete, matching source snapshots")


def run_file_workflow(
    file_path: str | Path, *, source_root: str | Path = ".", write_files: bool | None = None,
    run_root: str | Path | None = None, mode: Literal["review", "fix"] | None = None,
    task: str | None = None, active_agents: Sequence[str] | None = None,
    max_fix_attempts: int = 2, model=None, scaffold: bool = False,
    handlers: Mapping[str, NodeHandler] | None = None,
) -> WorkflowState:
    """Review a file; natural-language goals decide writes unless write_files overrides them."""
    from src.source_input import read_target, source_snapshot
    if write_files is not None and type(write_files) is not bool:
        raise ValueError("write_files must be a boolean or None")
    root, relative, source = read_target(file_path, source_root, run_root=run_root)
    absolute_target = str(file_path) if Path(file_path).is_absolute() else str(root/relative)
    return run_workflow(
        source_snapshot(relative, source), [relative], review_scope="file", target_file=absolute_target,
        file_contents={relative: source}, write_root=root if write_files else None,
        auto_write_root=root if task is not None and write_files is None else None,
        run_root=run_root, mode=mode, task=task, active_agents=active_agents,
        source_root=root,
        max_fix_attempts=max_fix_attempts, model=model, scaffold=scaffold, handlers=handlers,
    )


def run_task_workflow(
    task: str, *, source_root: str | Path = ".", run_root: str | Path | None = None,
    mode: Literal["review", "fix"] | None = None, active_agents: Sequence[str] | None = None,
    max_fix_attempts: int = 2, model=None, write_files: bool | None = None,
) -> WorkflowState:
    """Resolve explicit files/directories, dynamically review and optionally repair targets in place."""
    from src.task_intent import MAX_TASK_CHARS
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
        raise ValueError("task must contain 1 to 4000 characters")
    if mode not in (None, "review", "fix"):
        raise ValueError("mode must be review or fix")
    if type(max_fix_attempts) is not int or max_fix_attempts < 1:
        raise ValueError("max_fix_attempts must be a positive integer")
    if write_files is not None and type(write_files) is not bool:
        raise ValueError("write_files must be a boolean or None")
    if active_agents is not None:
        raise ValueError("Expert dispatch is dynamic; active_agents is no longer supported")
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source_root must be a directory")
    inputs = {"workflow_version": 3, "code_diff": "", "file_paths": [], "file_contents": {}, "mode": mode or "review",
              "active_agents": [], "max_fix_attempts": max_fix_attempts, "scaffold": False, "react": True,
              "user_task": redact(task), "requested_mode": mode,
              "review_scope": "file", "target_file": None, "resolve_target": True,
              "source_root": str(root), "run_history_root": str(Path(run_root).resolve()) if run_root is not None else None,
              "write_root": str(root) if write_files else None,
              "auto_write_root": str(root) if write_files is None else None}
    store = RunStore.create(run_root, inputs) if run_root is not None else None
    return _execute(inputs, store, model=model)


def resume_workflow(run_id: str, *, run_root="output/runs", model=None, handlers=None,
                    allow_code_change=False, additional_fix_attempts=1) -> WorkflowState:
    """Resume pending tasks, or start a new repair attempt after a rejected proposal."""
    if type(additional_fix_attempts) is not int or additional_fix_attempts < 1:
        raise ValueError("additional_fix_attempts must be a positive integer")
    store = RunStore.open(run_root, run_id)
    inputs = store.read_inputs()
    if inputs.get("resolve_target") and not (store.path/"checkpoint.sqlite").is_file() and any(
        event["node"] == "task_interpreter" and event["event"] == "node_completed" for event in store.read_events()
    ):
        raise ValueError("Saved target checkpoint is missing; start a new run instead of resolving another target")
    if not inputs.get("resolve_target"):
        validate_source_context(inputs["file_contents"], parse_diff(inputs["code_diff"]))
        _validate_file_scope(inputs["code_diff"], inputs["file_paths"], inputs["file_contents"], inputs.get("review_scope", "diff"))
    if inputs.get("workflow_version") != 3:
        raise ValueError("Legacy dispatch/checkpoint layout is incompatible; start a new dynamically planned run")
    return _execute(inputs, store, model=model, handlers=handlers, resume=True,
                    allow_code_change=allow_code_change, additional_fix_attempts=additional_fix_attempts)


def _initial_state(inputs, run_id, run_dir=""):
    return {key: value for key, value in inputs.items() if key != "scaffold"} | {
        "run_id": run_id, "run_dir": run_dir, "input_hash": digest(inputs),
        "source_hashes": {path: hashlib.sha256(source.encode()).hexdigest() for path, source in inputs["file_contents"].items()},
        "reports": {}, "findings": [], "fix_attempts": 0, "pending_steps": [], "errors": [],
        "review_plan": [], "observed_sources": {}, "review_round": 0, "review_disputes": [], "dispute_reviews": [],
        "failure_diagnosis": {}, "failure_diagnoses": [],
        "write_result": {"status": "not_run", "files": []},
        "runtime_validation": ValidationResult(summary="No runtime code or tests are executed by this project.", scope="runtime"),
    }


def _model_name(model, scaffold):
    if scaffold:
        return "scaffold"
    if model is None:
        return "uninitialized"
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    return name if isinstance(name, str) else type(model).__name__


def _checkpointer(store):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    path = store.path/"checkpoint.sqlite"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    serde = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[
        ("src.contracts", name) for name in ("AgentReport", "Finding", "FixPlan", "PatchArtifact", "ValidationResult")
    ])
    connection = sqlite3.connect(path, check_same_thread=False)
    return connection, SqliteSaver(connection, serde=serde)


def _execute(inputs, store, *, model=None, handlers=None, resume=False, allow_code_change=False, additional_fix_attempts=1):
    from src.runtime.store import file_lock
    with file_lock(store.path/".run.lock") if store else nullcontext():
        if resume:
            store = RunStore.open(store.path.parent, store.run_id)
            if store.metadata["status"] in {"completed", "proposed", "unsupported", "needs_clarification"}:
                raise ValueError("This run has already completed; start a new run instead")
            if store.metadata["code_hash"] != code_fingerprint() and not allow_code_change:
                raise ValueError("Workflow code changed; inspect compatibility and use --allow-code-change to resume explicitly")
            if allow_code_change and store.metadata["code_hash"] != code_fingerprint():
                store.metadata.setdefault("code_history", []).append(store.metadata["code_hash"])
                store.metadata["code_hash"] = code_fingerprint()
            store.metadata["resumes"] += 1
            store.metadata["status"] = "running"
            store.save_metadata()
        run_id = store.run_id if store else new_run_id()
        state = _initial_state(inputs, run_id, str(store.path) if store else "")
        tracer = TraceRecorder(run_id, store)
        if store:
            store.session_id = tracer.session_id
            store.metadata["session_id"] = tracer.session_id
            store.metadata["dependencies"] = {name: version(name) for name in (
                "langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core", "langchain-deepseek", "pydantic", "python-dotenv")}
            store.metadata.setdefault("sessions", []).append({"session_id": tracer.session_id,
                "model_name": "initializing", "code_hash": store.metadata["code_hash"],
                "dependencies": store.metadata["dependencies"], "pid": os.getpid()})
            store.save_metadata()
        tracer.emit("run_resumed" if resume else "run_started", input_hash=state["input_hash"])
        if resume:
            tracer.record_interrupted_spans()
        graph = None
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 48+12*state["max_fix_attempts"]}
        connection = None
        initialized = False
        try:
            if not inputs["scaffold"] and model is None:
                from src.llm import get_fast_llm
                with tracer.node("model_initialization", state):
                    try:
                        model = get_fast_llm()
                    except ValueError:
                        from src.model_agent import ModelOutputError
                        raise ModelOutputError("Check DEEPSEEK_API_KEY and model configuration",
                                               code="MODEL_CONFIGURATION_INVALID", stage="initialization") from None
            initialized = True
            state["model_name"] = _model_name(model, inputs["scaffold"])
            if store:
                store.metadata["sessions"][-1]["model_name"] = state["model_name"]
                store.save_metadata()
            if store:
                connection, checkpointer = _checkpointer(store)
            else:
                checkpointer = None
            # Keep historical runs on their saved strategy when resuming checkpoints.
            graph = build_workflow_graph(handlers, model=model, scaffold=inputs["scaffold"],
                                         checkpointer=checkpointer, tracer=tracer, react=inputs.get("react", False))
            invoke_input = state
            finished_snapshot = False
            if resume:
                snapshot = graph.get_state(config)
                if inputs.get("resolve_target") and not snapshot.values and any(
                    event["node"] == "task_interpreter" and event["event"] == "node_completed" for event in tracer.events
                ):
                    from src.model_agent import ModelOutputError
                    raise ModelOutputError("Saved target snapshot is missing; start a new run",
                                           code="REQUEST_SNAPSHOT_MISSING", stage="checkpoint_validation")
                if snapshot.values:
                    state = dict(snapshot.values)
                    if inputs.get("resolve_target") and state.get("file_paths"):
                        validate_source_context(state["file_contents"], parse_diff(state["code_diff"]))
                        _validate_file_scope(state["code_diff"], state["file_paths"], state["file_contents"], "file")
                    state["model_name"] = _model_name(model, inputs["scaffold"])
                    if snapshot.next:
                        invoke_input = None
                    elif state.get("status") in {"completed", "proposed"} and state.get("final_review"):
                        finished_snapshot = True
                    elif state.get("mode") == "fix" and state.get("findings") and state.get("validation"):
                        # Let begin_fix_attempt capture feedback before clearing artifacts,
                        # exactly as it does for retries within an uninterrupted run.
                        state["max_fix_attempts"] = state["fix_attempts"]+additional_fix_attempts
                        from langgraph.types import Overwrite
                        values = {key: state[key] for key in ("max_fix_attempts", "model_name")}
                        values.update(errors=Overwrite([]), pending_steps=Overwrite([]))
                        graph.update_state(config, values, as_node="adjudicator")
                        config["recursion_limit"] = 48+12*state["max_fix_attempts"]
                        invoke_input = None
                    else:
                        raise ValueError("Run has no resumable pending task or repair; start a new run")
            if not finished_snapshot:
                state = graph.invoke(invoke_input, config=config, **({"durability": "sync"} if store else {}))
        except (Exception, KeyboardInterrupt) as exc:
            if not store:
                raise
            if graph is not None:
                try:
                    snapshot = graph.get_state(config)
                    if snapshot.values:
                        state = dict(snapshot.values)
                except Exception as checkpoint_error:
                    tracer.emit("node_failed", stage="checkpoint_read", error_code="CHECKPOINT_READ_FAILED",
                                exception_type=type(checkpoint_error).__name__, locations=locations(checkpoint_error))
            node = exc.node if isinstance(exc, NodeExecutionError) else "model_initialization" if not initialized else "service"
            code = getattr(exc, "code", "RUN_EXECUTION_FAILED") if type(exc).__module__.startswith("src.") else "RUN_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "RUN_EXECUTION_FAILED"
            if not tracer.diagnostics() or tracer.diagnostics()[-1]["node"] != node:
                tracer.emit("node_failed", node=node, stage="initialization" if not initialized else "workflow",
                            error_code=code, exception_type=type(exc).__name__, locations=locations(exc))
            state["errors"] = [f"{node}: {code}; inspect diagnostics.json and events.jsonl"]
            if node in AGENT_IDS:
                state["reports"] = {**state.get("reports", {}), node: AgentReport(node, "failed")}
            if state.get("write_root"):
                from src.file_tools import read_write_result
                state["write_result"] = read_write_result(state["write_root"], store.path, state.get("candidate_files", {}))
            state.update(reporter_node(state))
        finally:
            if connection:
                connection.close()
        state["run_id"], state["run_dir"] = run_id, str(store.path) if store else ""
        state["model_name"] = _model_name(model, inputs["scaffold"])
        state["diagnostics"] = tracer.diagnostics()
        tracer.emit("run_finished", status=state.get("status", "failed"))
        state["trace_events"] = tracer.events
        state["context_hashes"] = {path: hashlib.sha256(source.encode()).hexdigest()
                                   for sources in state.get("observed_sources", {}).values()
                                   for path, source in sources.items() if path not in state.get("file_contents", {})}
        state.update(reporter_node(state))
        if store:
            store.metadata["completion_id"] = tracer.session_id
            store.finish(state)
        return state
