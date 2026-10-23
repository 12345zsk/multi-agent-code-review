"""Group A: review fan-out/fan-in and a bounded repair/verification loop."""

from collections.abc import Mapping
from functools import partial
from contextlib import nullcontext

from langgraph.graph import END, START, StateGraph

from src.contracts import AgentReport, FixPlan, NodeHandler, ValidationResult, WorkflowState
from src.repair import nodes as repair
from src.review.nodes import adjudicator_node, make_specialist_node
from src.task_intent import task_interpreter_node
from src.workflow.nodes import (
    context_collector_node,
    reporter_node,
    review_planner_node,
)
from src.workflow.registry import AGENT_IDS, REVIEW_AGENTS
from src.runtime.tracing import NodeExecutionError, error_code, trace_detail


def _blocked(state: WorkflowState) -> bool:
    return bool(state.get("pending_steps") or state.get("errors"))


def _safe_node(name: str, handler: NodeHandler, tracer=None) -> NodeHandler:
    def invoke(state: WorkflowState) -> dict:
        try:
            with tracer.node(name, state) if tracer else nullcontext() as span:
                update = handler(state)
                if name in AGENT_IDS:
                    if set(update) - {"reports", "pending_steps", "errors"}:
                        raise ValueError("Specialists must only update reduced fields")
                    report = update.get("reports", {}).get(name)
                    if not isinstance(report, AgentReport) or report.agent_id != name:
                        raise ValueError("Specialist must return its own AgentReport")
                    if set(update["reports"]) != {name}:
                        raise ValueError("Specialists cannot overwrite another report")
                if span:
                    found = [f.id for f in update.get("findings", [])]
                    found.extend(f.id for report in update.get("reports", {}).values() for f in report.findings)
                    span.finding_ids = list(dict.fromkeys([*span.finding_ids, *found]))
                    for key in ("validation", "regression"):
                        result = update.get(key)
                        if result and result.status == "failed":
                            span.status = "failed"
                            trace_detail("validation_failed", stage=result.stage or key,
                                         error_code=result.code or ("STATIC_VALIDATION_FAILED" if key == "validation" else "REGRESSION_REJECTED"),
                                         file_path=result.file_path, line=result.line, checks=list(result.checks))
                    if tracer.store and any(key in update for key in ("patch", "test_patch")):
                        records = tracer.store.save_drafts({**state, **update}, stage=name)
                        trace_detail("draft_saved", artifacts=records)
                return update
        except Exception as exc:
            if tracer and tracer.store:
                raise NodeExecutionError(name, error_code(exc)) from None
            # Exception messages can include provider credentials or source text.
            update = {"errors": [f"{name}: {type(exc).__name__}"]}
            if name in AGENT_IDS:
                update["reports"] = {name: AgentReport(name, status="failed")}
            return update

    return invoke


def _route_reviews(state: WorkflowState) -> list[str]:
    selected = [agent for agent in state["active_agents"] if agent in AGENT_IDS]
    return selected or ["adjudicator"]


def _route_task(state: WorkflowState) -> str:
    if _blocked(state) or state.get("task_intent", {}).get("status") in {"unsupported", "needs_clarification"}:
        return "reporter"
    return "context_collector"


def _route_findings(state: WorkflowState) -> str:
    complete = all(
        state.get("reports", {}).get(agent, AgentReport(agent)).status == "completed"
        for agent in state["active_agents"]
    )
    if state["mode"] == "fix" and complete and not _blocked(state) and any(
        finding.status == "confirmed" and finding.fixable
        for finding in state.get("findings", [])
    ):
        return "fix_planner"
    return "reporter"


def _route_plan(state: WorkflowState) -> str:
    plan = state.get("fix_plan")
    return "patch_generator" if plan and plan.status == "ready" and not _blocked(state) else "reporter"


def _route_patch(state: WorkflowState) -> str:
    patch = state.get("patch")
    if patch and patch.status == "generated" and patch.diff.strip() and not _blocked(state):
        return "test_generator"
    return "reporter"


def _route_validation(state: WorkflowState) -> str:
    if _blocked(state):
        return "reporter"
    result = state.get("validation", ValidationResult())
    if result.status == "passed" or (result.status == "not_run" and result.summary):
        return "regression_reviewer"
    if result.status == "failed" and state["fix_attempts"] < state["max_fix_attempts"]:
        return "fix_planner"
    return "reporter"


def _route_regression(state: WorkflowState) -> str:
    result = state.get("regression", ValidationResult())
    if not _blocked(state) and result.status == "failed" and state["fix_attempts"] < state["max_fix_attempts"]:
        return "fix_planner"
    if (not _blocked(state) and result.status == "passed" and state.get("write_root")
            and state.get("validation", ValidationResult()).status == "passed"):
        return "file_writer"
    return "reporter"


def build_workflow_graph(handlers: Mapping[str, NodeHandler] | None = None, *, model=None, scaffold=False,
                         checkpointer=None, tracer=None, react=True):
    """Compile live model nodes, or explicit offline scaffold nodes.

    Each specialist runs as one outer node, including its model invocation.
    All selected branches therefore join at the adjudicator in one superstep.
    """
    nodes = {
        "task_interpreter": partial(task_interpreter_node, model=model),
        "context_collector": context_collector_node,
        "review_planner": review_planner_node,
        **{spec.id: make_specialist_node(spec, model, react=react) for spec in REVIEW_AGENTS},
        "adjudicator": partial(adjudicator_node, model=model),
        "fix_planner": partial(repair.fix_planner_node, model=model),
        "patch_generator": partial(repair.patch_generator_node, model=model),
        "test_generator": partial(repair.test_generator_node, model=model),
        "validator": repair.validator_node,
        "regression_reviewer": partial(repair.regression_reviewer_node, model=model),
        "file_writer": repair.file_writer_node,
        "reporter": reporter_node,
    }
    if scaffold:
        from src.workflow.scaffold import scaffold_handlers
        nodes.update(scaffold_handlers())
    overrides = dict(handlers or {})
    unknown = overrides.keys() - nodes.keys()
    if unknown:
        raise ValueError(f"Unknown workflow nodes: {', '.join(sorted(unknown))}")
    nodes.update(overrides)
    planner = _safe_node("fix_planner", nodes["fix_planner"], tracer)

    def begin_fix_attempt(state: WorkflowState) -> dict:
        # Clear artifacts from earlier attempts so stale success cannot leak.
        current = {
            **state,
            "previous_attempt": {
                "patch": state["patch"].diff if state.get("patch") else "",
                "validation": state.get("validation", ValidationResult()).summary,
                "regression": state.get("regression", ValidationResult()).summary,
            },
            "fix_attempts": state["fix_attempts"] + 1,
            "fix_plan": FixPlan(),
            "patch": None,
            "test_patch": None,
            "validation": ValidationResult(),
            "regression": ValidationResult(scope="model"),
            "candidate_files": {},
        }
        return {
            "fix_plan": current["fix_plan"],
            **planner(current),
            **{key: current[key] for key in (
                "fix_attempts", "patch", "test_patch", "validation", "regression", "candidate_files", "previous_attempt"
            )},
        }

    graph = StateGraph(WorkflowState)
    for name, handler in nodes.items():
        graph.add_node(name, begin_fix_attempt if name == "fix_planner" else _safe_node(name, handler, tracer))
    graph.add_edge(START, "task_interpreter")
    graph.add_conditional_edges("task_interpreter", _route_task, ["context_collector", "reporter"])
    graph.add_edge("context_collector", "review_planner")
    graph.add_conditional_edges("review_planner", _route_reviews, [*AGENT_IDS, "adjudicator"])
    for agent in AGENT_IDS:
        graph.add_edge(agent, "adjudicator")
    graph.add_conditional_edges("adjudicator", _route_findings, ["fix_planner", "reporter"])
    graph.add_conditional_edges("fix_planner", _route_plan, ["patch_generator", "reporter"])
    graph.add_conditional_edges("patch_generator", _route_patch, ["test_generator", "reporter"])
    graph.add_edge("test_generator", "validator")
    graph.add_conditional_edges("validator", _route_validation, ["regression_reviewer", "fix_planner", "reporter"])
    graph.add_conditional_edges("regression_reviewer", _route_regression, ["fix_planner", "file_writer", "reporter"])
    graph.add_edge("file_writer", "reporter")
    graph.add_edge("reporter", END)
    return graph.compile(checkpointer=checkpointer)
