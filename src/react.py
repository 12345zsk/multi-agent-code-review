"""Bounded ReAct subgraph with authorized project discovery and snapshot reads."""

import json
from typing import TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from src.model_agent import ModelOutputError, json_messages, parse_json_response
from src.runtime.tracing import digest, error_code, trace_detail, trace_stage
from src.serialization import to_data

MAX_TOOL_CALLS = 8
MAX_MODEL_REQUESTS = MAX_TOOL_CALLS + 2


def make_read_source(sources):
    """Compatibility factory for standalone snapshot reads; workflow tools use ProjectContext."""
    from src.project_tools import ProjectContext, make_project_tools
    return make_project_tools(ProjectContext(to_data(dict(sources))))["read_source"]


class ReactState(TypedDict):
    messages: list[AnyMessage]
    calls: int
    steps: int
    format_attempt: int
    pending_call: dict | None
    remaining_calls: list[dict]
    finalizing: bool
    result: dict | None
    observed_sources: dict[str, str]
    tool_observations: list[dict]


def invoke_react_json(model, role, instructions, payload, schema, *, sources,
                      source_root=None, inventory=None, run_dir=None, observed_sources=None, review_diff=None,
                      tool_observations=None):
    """Run model -> registered project tools -> model, then validate the final schema.

    Model and tool execution are separate graph nodes, so a parent's durable
    checkpointer can resume after a completed tool without repeating the read.
    """
    from src.project_tools import ProjectContext, make_project_tools
    context = ProjectContext(sources, root=source_root, inventory=inventory, run_dir=run_dir, review_diff=review_diff)
    tools = make_project_tools(context)
    try:
        bound = model.bind_tools(list(tools.values()), parallel_tool_calls=False)
    except Exception:
        raise ModelOutputError("Model must support tool binding", code="MODEL_TOOL_BINDING_FAILED", stage="tool_binding") from None

    def model_node(state):
        step = state["steps"]+1
        if step > MAX_MODEL_REQUESTS:
            raise ModelOutputError("ReAct step budget exhausted", code="REACT_STEP_LIMIT_EXCEEDED", stage="react_budget")
        remaining = MAX_TOOL_CALLS-state["calls"]
        # Reserve the final two model requests for JSON plus its one format retry.
        finalizing = state.get("finalizing", False) or remaining == 0 or step > MAX_TOOL_CALLS
        budget_note = (f"\nRuntime budget: remaining_tool_calls={remaining}; "
                       f"remaining_model_requests={MAX_MODEL_REQUESTS-step+1}. "
                       "Each call in a batch counts separately. Avoid repeated searches with no new evidence. ")
        if finalizing:
            budget_note += ("Context collection has ended. Do not request tools. Return the final JSON now using "
                            "only collected evidence. Preserve uncertainty; missing evidence is not proof that "
                            "tests or defects are absent. Do not invent findings or claim complete coverage.")
            trace_detail("react_finalization", react_step=step, calls_used=state["calls"],
                         remaining_tool_calls=remaining)
        else:
            budget_note += "Stay within the remaining calls and finish once evidence is sufficient."
        # Refresh the system budget without growing the persisted message history.
        messages = [state["messages"][0].model_copy(update={"content": state["messages"][0].content+budget_note}),
                    *state["messages"][1:]]
        try:
            with trace_stage("model_request", role=role, react_step=step, request_hash=digest(
                [message.model_dump() for message in messages]
            )):
                # No tools are supplied to the provider during finalization.
                response = (model if finalizing else bound).invoke(messages)
        except Exception as exc:
            # Child checkpoints may persist exception strings: keep them controlled.
            raise ModelOutputError("ReAct model request failed", code=error_code(exc), stage="model_request") from None
        if not isinstance(response, AIMessage) or response.invalid_tool_calls:
            raise ModelOutputError("Unsupported tool response", code="MODEL_RESPONSE_UNSUPPORTED", stage="response_validation")
        if response.usage_metadata:
            trace_detail("model_usage", react_step=step, **{
                key: response.usage_metadata.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")
            })
        tool_names = [call["name"] if call["name"] in tools else "<unregistered>" for call in response.tool_calls]
        trace_detail("react_step", step=step, action=tool_names[0] if tool_names else "final_answer",
                     tool_names=tool_names, tool_call_count=len(tool_names))
        if response.tool_calls:
            calls = to_data(response.tool_calls)
            if any(call["name"] not in tools for call in calls):
                raise ModelOutputError("Only registered project tools are allowed", code="TOOL_NOT_ALLOWED", stage="tool_validation")
            used_ids = {item["id"] for message in state["messages"] if isinstance(message, AIMessage)
                        for item in message.tool_calls}
            for call in calls:
                if not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 200 or call["id"] in used_ids:
                    raise ModelOutputError("Invalid tool call ID", code="TOOL_CALL_ID_INVALID", stage="tool_validation")
                used_ids.add(call["id"])
                if len(json.dumps(call)) > 16000:
                    raise ModelOutputError("Tool arguments exceed budget", code="TOOL_ARGUMENTS_TOO_LARGE", stage="tool_validation")
            if finalizing:
                raise ModelOutputError("Final JSON required after context collection ended",
                                       code="REACT_FINAL_ANSWER_REQUIRED", stage="response_validation")
            if len(calls) > remaining:
                trace_detail("tool_batch_rejected", react_step=step, error_code="REACT_TOOL_LIMIT_EXCEEDED",
                             requested_calls=len(calls), remaining_tool_calls=remaining)
                # Deny the entire batch and answer every call ID before asking for
                # final JSON. No rejected call executes or consumes tool budget.
                denied = json.dumps({"status": "error", "code": "REACT_TOOL_LIMIT_EXCEEDED",
                                     "remaining_tool_calls": remaining, "final_answer_required": True})
                return {"messages": [*state["messages"], AIMessage(content="", tool_calls=calls),
                                     *[ToolMessage(content=denied, tool_call_id=call["id"]) for call in calls]],
                        "pending_call": None, "remaining_calls": [], "steps": step, "finalizing": True}
            # Reserve the whole batch before execution; each tool gets its own checkpoint.
            # Keep pending_call so existing single-call checkpoints remain resumable.
            return {"messages": [*state["messages"], AIMessage(content="", tool_calls=calls)],
                    "pending_call": calls[0], "remaining_calls": calls[1:],
                    "calls": state["calls"]+len(calls), "steps": step}
        try:
            reply = parse_json_response(response, schema, format_attempt=state["format_attempt"])
        except (ValidationError, json.JSONDecodeError) as exc:
            if isinstance(exc, ValidationError):
                trace_detail("schema_issues", fields=[{"location": list(error["loc"]), "type": error["type"]}
                                                     for error in exc.errors(include_input=False, include_context=False)])
            if state["format_attempt"] >= 2:
                raise ModelOutputError("Final response did not match schema",
                                       code="MODEL_SCHEMA_INVALID" if isinstance(exc, ValidationError) else "MODEL_JSON_INVALID",
                                       stage="schema_validation" if isinstance(exc, ValidationError) else "json_parse") from None
            return {"messages": [*state["messages"], HumanMessage(content=
                    "Your final answer was invalid JSON or violated the schema. Return a corrected JSON object matching the original schema.")],
                    "format_attempt": 2, "steps": step, "pending_call": None, "remaining_calls": [],
                    "finalizing": finalizing}
        return {"result": reply.model_dump(), "steps": step, "pending_call": None, "remaining_calls": []}

    def tool_node(state):
        call = state["pending_call"]
        context.sources.update(state.get("observed_sources", {}))
        metadata = {"tool": call["name"], "tool_call_id": call["id"], "react_step": state["steps"]}
        with trace_stage("tool_execution", **metadata):
            trace_detail("tool_started", **metadata, arguments_hash=digest(call["args"]))
            try:
                observation = tools[call["name"]].invoke(call["args"])
            except ValueError:
                observation = {"status": "error", "code": "TOOL_ARGUMENTS_INVALID"}
            except Exception:
                raise ModelOutputError("Source tool execution failed", code="TOOL_EXECUTION_FAILED", stage="tool_execution") from None
            trace_detail("tool_completed", **metadata, status=observation["status"],
                         error_code=observation.get("code", ""), result_hash=digest(observation),
                         source_hash=observation.get("source_hash", ""))
        remaining = state.get("remaining_calls", [])
        # Retain search limitations for downstream adjudication, without query
        # text, source excerpts or raw arguments in public evidence metadata.
        check = {"tool": call["name"], "status": observation["status"],
                 "truncated": observation.get("truncated", False),
                 "unavailable": observation.get("unavailable", []),
                 "code": observation.get("code", "")}
        if call["name"] == "search_code":
            check["match_count"] = len(observation.get("matches", []))
        return {"messages": [*state["messages"], ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False), tool_call_id=call["id"])],
                "pending_call": remaining[0] if remaining else None, "remaining_calls": remaining[1:],
                "observed_sources": dict(context.sources),
                "tool_observations": [*state.get("tool_observations", []), check]}

    def route(state):
        return END if state.get("result") is not None else "tools" if state.get("pending_call") else "model"

    builder = StateGraph(ReactState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route, ["tools", "model", END])
    builder.add_conditional_edges("tools", route, ["tools", "model"])
    graph = builder.compile()
    result = graph.invoke({"messages": json_messages(role, instructions, payload, schema, allow_tools=True),
                           "calls": 0, "steps": 0, "format_attempt": 1, "pending_call": None, "remaining_calls": [], "result": None,
                           "observed_sources": dict(sources), "tool_observations": [], "finalizing": False})
    if observed_sources is not None:
        observed_sources.update(result.get("observed_sources", {}))
    if tool_observations is not None:
        tool_observations.extend(result.get("tool_observations", []))
    return schema.model_validate(result["result"])
