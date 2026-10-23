"""Structured execution events without prompts, model responses or exception text."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from threading import RLock
from time import perf_counter
import traceback
from uuid import uuid4

from src.serialization import to_data


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(value):
    return hashlib.sha256(json.dumps(to_data(value), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def error_code(exc):
    if type(exc).__module__.startswith("src.") and getattr(exc, "code", None):
        return exc.code
    name = type(exc).__name__.lower()
    for fragment, code in (("timeout", "MODEL_TIMEOUT"), ("connection", "MODEL_CONNECTION_FAILED"),
                           ("ratelimit", "MODEL_RATE_LIMITED"), ("authentication", "MODEL_AUTHENTICATION_FAILED")):
        if fragment in name:
            return code
    return "NODE_EXECUTION_FAILED"


def locations(exc):
    # Frames identify executable code, never source lines, local variables or messages.
    return [{"module": frame.f_globals.get("__name__", ""), "function": frame.f_code.co_name, "line": line}
            for frame, line in traceback.walk_tb(exc.__traceback__)][-12:]


@dataclass
class NodeSpan:
    recorder: "TraceRecorder"
    node: str
    attempt: int
    span_id: str = field(default_factory=lambda: uuid4().hex)
    finding_ids: list[str] = field(default_factory=list)
    failed_stage: str = "execution"
    status: str = "completed"


_SPAN: ContextVar[NodeSpan | None] = ContextVar("review_execution_span", default=None)


@contextmanager
def trace_stage(stage, **metadata):
    span = _SPAN.get()
    started = perf_counter()
    if span:
        span.recorder.emit("stage_started", span, stage=stage, **metadata)
    try:
        yield
    except BaseException as exc:
        if span:
            span.failed_stage = stage
            span.recorder.emit("stage_failed", span, stage=stage, duration_ms=round((perf_counter()-started)*1000, 3),
                               error_code=error_code(exc), exception_type=type(exc).__name__, locations=locations(exc), **metadata)
        raise
    else:
        if span:
            span.recorder.emit("stage_completed", span, stage=stage, duration_ms=round((perf_counter()-started)*1000, 3), **metadata)


def trace_detail(event, **metadata):
    span = _SPAN.get()
    if span:
        span.recorder.emit(event, span, **metadata)


class NodeExecutionError(RuntimeError):
    """A safe exception that leaves LangGraph's failed task pending for resume."""

    def __init__(self, node, code):
        super().__init__(f"{node}: {code}")
        self.node, self.code = node, code


class TraceRecorder:
    def __init__(self, run_id, store=None):
        self.run_id = run_id
        self.store = store
        self.session_id = uuid4().hex
        self.events = store.read_events(repair=True) if store else []
        self._lock = RLock()

    def emit(self, event, span=None, **metadata):
        with self._lock:
            item = to_data({
                "sequence": len(self.events)+1, "event_id": uuid4().hex,
                "run_id": self.run_id, "session_id": self.session_id, "timestamp": timestamp(),
                "event": event, "node": span.node if span else "service",
                "span_id": span.span_id if span else None, "attempt": span.attempt if span else 0,
                "finding_ids": span.finding_ids if span else [], **metadata,
            })
            if self.store:
                self.store.append_event(item)
            self.events.append(item)
            return item

    @contextmanager
    def node(self, name, state):
        span = NodeSpan(self, name, state.get("fix_attempts", 0), finding_ids=[f.id for f in state.get("findings", [])])
        token = _SPAN.set(span)
        started = perf_counter()
        self.emit("node_started", span, input_hash=digest({key: value for key, value in state.items() if key not in {"trace_events", "diagnostics", "run_dir"}}))
        try:
            yield span
        except BaseException as exc:
            stage = getattr(exc, "stage", span.failed_stage)
            if stage == "output_validation" and span.failed_stage != "execution":
                stage = span.failed_stage
            self.emit("node_failed", span, stage=stage,
                      error_code=error_code(exc), exception_type=type(exc).__name__, locations=locations(exc),
                      duration_ms=round((perf_counter()-started)*1000, 3))
            raise
        else:
            self.emit("node_completed" if span.status == "completed" else "node_finished", span,
                      status=span.status, duration_ms=round((perf_counter()-started)*1000, 3))
        finally:
            _SPAN.reset(token)

    def diagnostics(self):
        failures = [event for event in self.events if event["event"] in {"node_failed", "validation_failed", "node_interrupted"}]
        result = []
        for event in failures:
            resolved = any(later["sequence"] > event["sequence"] and later["node"] == event["node"]
                           and later["event"] == "node_completed" for later in self.events)
            result.append({**event, "resolved": resolved})
        return result

    def record_interrupted_spans(self):
        starts = {event["span_id"]: event for event in self.events if event["event"] == "node_started"}
        ended = {event["span_id"] for event in self.events if event["event"] in {"node_completed", "node_failed", "node_finished", "node_interrupted"}}
        for span_id, started in starts.items():
            if span_id in ended:
                continue
            stage = next((event.get("stage", "execution") for event in reversed(self.events)
                          if event.get("span_id") == span_id and event.get("stage")), "execution")
            span = NodeSpan(self, started["node"], started["attempt"], span_id=span_id, finding_ids=started.get("finding_ids", []))
            self.emit("node_interrupted", span, stage=stage, error_code="PROCESS_INTERRUPTED",
                      previous_session_id=started["session_id"])
