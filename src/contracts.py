"""Shared contracts for the three development teams; no model or I/O imports."""

import operator
from dataclasses import dataclass
from typing import Annotated, Callable, Literal, TypedDict

FindingCategory = Literal[
    "security.sql_injection", "security.authorization", "security.hardcoded_credential",
    "correctness.empty_input", "correctness.pagination", "correctness.shared_state",
    "correctness.index_bounds", "tests.missing_coverage", "performance", "architecture",
    "compatibility", "dependency", "maintainability", "other",
]


@dataclass(frozen=True)
class AgentSpec:
    id: str
    responsibility: str
    owner: str = "group-b"


@dataclass(frozen=True)
class Finding:
    id: str
    agent_id: str
    title: str
    description: str
    file_path: str
    line: int | None = None
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    status: Literal["candidate", "confirmed", "rejected"] = "candidate"
    fixable: bool = False
    suggestion: str = ""
    evidence: str = ""
    adjudication: str = ""
    severity_reason: str = ""
    critical_impact_evidence: str = ""
    critical_exposure_evidence: str = ""
    category: FindingCategory = "other"


@dataclass(frozen=True)
class AgentReport:
    agent_id: str
    status: Literal["pending", "completed", "failed"] = "pending"
    findings: tuple[Finding, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class FixPlan:
    status: Literal["pending", "ready"] = "pending"
    finding_ids: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class PatchArtifact:
    """A proposed patch, never evidence that files were changed or tests passed."""

    status: Literal["pending", "generated", "not_needed"] = "pending"
    diff: str = ""
    finding_ids: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ValidationResult:
    status: Literal["not_run", "passed", "failed"] = "not_run"
    checks: tuple[str, ...] = ()
    summary: str = ""
    scope: Literal["static", "model", "runtime"] = "static"
    code: str = ""
    stage: str = ""
    file_path: str = ""
    line: int | None = None


class WorkflowState(TypedDict, total=False):
    run_id: str
    run_dir: str
    input_hash: str
    source_hashes: dict[str, str]
    model_name: str
    react: bool
    write_root: str | None
    write_result: dict
    diagnostics: list[dict]
    trace_events: list[dict]
    code_diff: str
    file_paths: list[str]
    mode: Literal["review", "fix"]
    user_task: str
    requested_mode: Literal["review", "fix"] | None
    requested_agents: list[str] | None
    task_intent: dict
    active_agents: list[str]
    context: dict[str, str]
    file_contents: dict[str, str]
    candidate_files: dict[str, str]
    previous_attempt: dict[str, str]
    # Independent parallel branches own distinct agent IDs. Retry stages do not
    # append old findings; the adjudicator replaces the canonical findings list.
    reports: Annotated[dict[str, AgentReport], operator.or_]
    findings: list[Finding]
    fix_plan: FixPlan
    patch: PatchArtifact | None
    test_patch: PatchArtifact | None
    validation: ValidationResult
    regression: ValidationResult
    runtime_validation: ValidationResult
    fix_attempts: int
    max_fix_attempts: int
    pending_steps: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    status: Literal["completed", "proposed", "incomplete", "failed", "unsupported", "needs_clarification"]
    final_review: str


# A handler returns only changed fields, never the whole shared state. In
# parallel review nodes, only `reports`, `pending_steps`, and `errors` may change.
NodeHandler = Callable[[WorkflowState], dict]
