"""Minimal, bounded ReAct subgraph with one read-only source-snapshot tool."""

import hashlib
import json
from pathlib import PurePosixPath
from typing import TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.diff import safe_path
from src.model_agent import ModelOutputError, json_messages, parse_json_response
from src.runtime.tracing import digest, error_code, trace_detail, trace_stage
from src.serialization import to_data

MAX_TOOL_CALLS = 3


class ReadSourceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str = Field(min_length=1, max_length=500)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def check_range(self):
        if not 0 <= self.end_line-self.start_line < 200:
            raise ValueError("Read at most 200 lines in ascending order")
        return self


def make_read_source(sources):
    # Capture only caller-provided, redacted input; never consult live files.
    snapshot = to_data(dict(sources))

    @tool(args_schema=ReadSourceArgs)
    def read_source(file_path: str, start_line: int = 1, end_line: int = 200) -> dict:
        """Read up to 200 numbered lines from an available post-change source snapshot."""
        path = safe_path(file_path)
        parts = PurePosixPath(path).parts
        if any(part.startswith(".env") for part in parts) or PurePosixPath(path).suffix in {".key", ".pem", ".p12"}:
            return {"status": "error", "code": "SOURCE_NOT_AVAILABLE"}
        if path not in snapshot:
            return {"status": "error", "code": "SOURCE_NOT_AVAILABLE"}
        source = snapshot[path]
        lines = source.splitlines()
        if start_line > max(1, len(lines)):
            return {"status": "error", "code": "LINE_RANGE_INVALID"}
        selected = [{"line": index, "text": text}
                    for index, text in enumerate(lines[start_line-1:end_line], start_line)]
        if sum(len(item["text"]) for item in selected) > 16000:
            return {"status": "error", "code": "TOOL_OUTPUT_TOO_LARGE"}
        return {"status": "ok", "file_path": path, "total_lines": len(lines), "lines": selected,
                "source_hash": hashlib.sha256(source.encode()).hexdigest()}

    return read_source


class ReactState(TypedDict):
    messages: list[AnyMessage]
    calls: int
    steps: int
    format_attempt: int
    pending_call: dict | None
    result: dict | None


def invoke_react_json(model, role, instructions, payload, schema, *, sources):
    """Run model -> read_source -> model, then validate the final JSON schema.

    Model and tool execution are separate graph nodes, so a parent's durable
    checkpointer can resume after a completed tool without repeating the read.
    """
    read_source = make_read_source(sources)
    try:
        bound = model.bind_tools([read_source], parallel_tool_calls=False)
    except Exception:
        raise ModelOutputError("Model must support tool binding", code="MODEL_TOOL_BINDING_FAILED", stage="tool_binding") from None

    def model_node(state):
        step = state["steps"]+1
        if step > MAX_TOOL_CALLS+2:
            raise ModelOutputError("ReAct step budget exhausted", code="REACT_STEP_LIMIT_EXCEEDED", stage="react_budget")
        try:
            with trace_stage("model_request", role=role, react_step=step, request_hash=digest(
                [message.model_dump() for message in state["messages"]]
            )):
                response = bound.invoke(state["messages"])
        except Exception as exc:
            # Child checkpoints may persist exception strings: keep them controlled.
            raise ModelOutputError("ReAct model request failed", code=error_code(exc), stage="model_request") from None
        if not isinstance(response, AIMessage) or response.invalid_tool_calls:
            raise ModelOutputError("Unsupported tool response", code="MODEL_RESPONSE_UNSUPPORTED", stage="response_validation")
        if response.usage_metadata:
            trace_detail("model_usage", react_step=step, **{
                key: response.usage_metadata.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")
            })
        trace_detail("react_step", step=step, action="read_source" if response.tool_calls else "final_answer")
        if response.tool_calls:
            calls = response.tool_calls
            if len(calls) != 1 or calls[0]["name"] != "read_source":
                raise ModelOutputError("Only one read_source call is allowed", code="TOOL_NOT_ALLOWED", stage="tool_validation")
            call = to_data(calls[0])
            used_ids = {item["id"] for message in state["messages"] if isinstance(message, AIMessage)
                        for item in message.tool_calls}
            if not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 200 or call["id"] in used_ids:
                raise ModelOutputError("Invalid tool call ID", code="TOOL_CALL_ID_INVALID", stage="tool_validation")
            if len(json.dumps(call)) > 16000:
                raise ModelOutputError("Tool arguments exceed budget", code="TOOL_ARGUMENTS_TOO_LARGE", stage="tool_validation")
            if state["calls"] >= MAX_TOOL_CALLS:
                raise ModelOutputError("ReAct tool budget exhausted", code="REACT_TOOL_LIMIT_EXCEEDED", stage="react_budget")
            # Preserve the native tool-call protocol, without hidden reasoning or prose.
            return {"messages": [*state["messages"], AIMessage(content="", tool_calls=[call])],
                    "pending_call": call, "calls": state["calls"]+1, "steps": step}
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
                    "format_attempt": 2, "steps": step, "pending_call": None}
        return {"result": reply.model_dump(), "steps": step, "pending_call": None}

    def tool_node(state):
        call = state["pending_call"]
        metadata = {"tool": "read_source", "tool_call_id": call["id"], "react_step": state["steps"]}
        with trace_stage("tool_execution", **metadata):
            trace_detail("tool_started", **metadata, arguments_hash=digest(call["args"]))
            try:
                observation = read_source.invoke(call["args"])
            except ValueError:
                observation = {"status": "error", "code": "TOOL_ARGUMENTS_INVALID"}
            except Exception:
                raise ModelOutputError("Source tool execution failed", code="TOOL_EXECUTION_FAILED", stage="tool_execution") from None
            trace_detail("tool_completed", **metadata, status=observation["status"],
                         error_code=observation.get("code", ""), result_hash=digest(observation),
                         source_hash=observation.get("source_hash", ""))
        return {"messages": [*state["messages"], ToolMessage(
                    content=json.dumps(observation, ensure_ascii=False), tool_call_id=call["id"])], "pending_call": None}

    def route(state):
        return END if state.get("result") is not None else "tools" if state.get("pending_call") else "model"

    builder = StateGraph(ReactState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route, ["tools", "model", END])
    builder.add_edge("tools", "model")
    graph = builder.compile()
    result = graph.invoke({"messages": json_messages(role, instructions, payload, schema, allow_tools=True),
                           "calls": 0, "steps": 0, "format_attempt": 1, "pending_call": None, "result": None})
    return schema.model_validate(result["result"])
