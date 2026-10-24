"""Strict JSON contracts for final review, adjudication and repair output."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.contracts import FindingCategory


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def meaningful_text(cls, value, info):
        if isinstance(value, str) and info.field_name != "diff" and not value.strip():
            raise ValueError("Text fields must contain non-whitespace content")
        return value


class FindingReply(Reply):
    category: FindingCategory = "other"
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=3000)
    file_path: str = Field(min_length=1, max_length=500)
    line: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "high", "medium", "low"]
    fixable: bool
    suggestion: str = Field(max_length=3000)
    evidence: str = Field(min_length=1, max_length=2000)


class ReviewReply(Reply):
    summary: str = Field(min_length=1, max_length=3000)
    findings: list[FindingReply] = Field(max_length=30)


class Decision(Reply):
    finding_id: str
    status: Literal["confirmed", "candidate", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    category: FindingCategory = "other"
    description: str = Field(min_length=1, max_length=3000)
    suggestion: str = Field(min_length=1, max_length=3000)
    severity: Literal["critical", "high", "medium", "low"]
    severity_reason: str = Field(min_length=1, max_length=2000)
    critical_impact_evidence: str | None = Field(default=None, min_length=1, max_length=2000)
    critical_exposure_evidence: str | None = Field(default=None, min_length=1, max_length=2000)
    duplicate_of: str | None = None


class AdjudicationReply(Reply):
    decisions: list[Decision] = Field(max_length=240)
    disputes: list["DisputeRequest"] = Field(default_factory=list, max_length=3)


class PlanReply(Reply):
    finding_ids: list[str] = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=5000)


class PatchReply(Reply):
    finding_ids: list[str] = Field(min_length=1, max_length=240)
    diff: str = Field(min_length=1, max_length=80000)
    summary: str = Field(min_length=1, max_length=3000)


class TestReply(Reply):
    status: Literal["generated", "not_needed"]
    diff: str = Field(max_length=80000)
    summary: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def check_diff(self):
        if (self.status == "generated") != bool(self.diff.strip()):
            raise ValueError("Generated tests require a diff; not_needed requires an empty diff")
        return self


class RegressionReply(Reply):
    status: Literal["passed", "failed", "not_run"]
    summary: str = Field(min_length=1, max_length=4000)


class ReviewAssignment(Reply):
    agent_id: Literal["bug_detector", "security", "code_quality", "test_coverage", "architecture", "performance", "dependency", "compatibility"]
    reason: str = Field(min_length=1, max_length=1000)
    questions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(min_length=1, max_length=5)


class ReviewPlanReply(Reply):
    assignments: list[ReviewAssignment] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_agents(self):
        if len({item.agent_id for item in self.assignments}) != len(self.assignments):
            raise ValueError("Duplicate specialist assignments")
        return self


class DisputeRequest(Reply):
    agent_id: Literal["bug_detector", "security", "code_quality", "test_coverage", "architecture", "performance", "dependency", "compatibility"]
    finding_ids: list[str] = Field(min_length=1, max_length=10)
    question: str = Field(min_length=1, max_length=1000)


class DisputeResolution(Reply):
    finding_id: str
    status: Literal["confirmed", "rejected", "candidate"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(max_length=2000)


class DisputeReply(Reply):
    resolutions: list[DisputeResolution] = Field(min_length=1, max_length=10)


class FailureDiagnosisReply(Reply):
    category: Literal["context_missing", "code_patch", "test_patch", "regression", "unsupported"]
    action: Literal["retry", "collect_context", "stop"]
    summary: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=3000)


AdjudicationReply.model_rebuild()
