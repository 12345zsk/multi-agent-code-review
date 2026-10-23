"""Structured, one-to-one scoring against human-authored defect labels."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts import FindingCategory
from src.diff import safe_path
from src.runtime.tracing import digest
from src.serialization import to_data


class ExpectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    category: FindingCategory
    file_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    fixable: bool = True

    @model_validator(mode="after")
    def check_location(self):
        self.file_path = safe_path(self.file_path)
        if self.line_end is not None and (self.line_start is None or self.line_end < self.line_start):
            raise ValueError("Invalid annotated line range")
        return self


class Labels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    case_id: str
    expected: list[ExpectedFinding] = Field(max_length=1000)
    line_tolerance: int = Field(default=0, ge=0, le=20)
    source_hashes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_labels(self):
        if len({item.id for item in self.expected}) != len(self.expected):
            raise ValueError("Expected finding IDs must be unique")
        return self


class Prediction(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    category: FindingCategory = "other"
    status: Literal["confirmed", "candidate", "rejected"]
    file_path: str
    line: int | None = Field(default=None, ge=1)


class HumanFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    finding_id: str
    expected_id: str | None
    reviewed: bool


class RepairAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_id: str
    status: Literal["passed", "failed"]
    method: Literal["human_review", "isolated_tests"]
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=2000)


class HumanAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    run_id: str
    result_hash: str
    findings: list[HumanFinding]
    repairs: list[RepairAssessment] = Field(default_factory=list)


def annotation_template(report):
    report = to_data(report)
    return {"schema_version": 1, "run_id": report["run_id"], "result_hash": digest(report),
            "findings": [{"finding_id": item["id"], "expected_id": None, "reviewed": False}
                         for item in report.get("findings", []) if item["status"] == "confirmed"], "repairs": []}


def _ratio(numerator, denominator):
    return numerator/denominator if denominator else None


def metrics(counts):
    tp, fp, fn = (counts[key] for key in ("true_positives", "false_positives", "false_negatives"))
    return {
        **counts, "precision": _ratio(tp, tp+fp), "recall": _ratio(tp, tp+fn),
        # This is FP / predicted positives, not FP / (FP + TN).
        "false_discovery_rate": _ratio(fp, tp+fp), "miss_rate": _ratio(fn, tp+fn),
        "f1": _ratio(2*tp, 2*tp+fp+fn),
        "localization_accuracy": _ratio(counts["localized_matches"], counts["location_assessed"]),
        "repair_success_rate": _ratio(counts["repair_passed"], counts["repair_assessed"]),
        "repair_verification_coverage": _ratio(counts["repair_assessed"], counts["expected_fixable"]),
        "clean_false_alarm_rate": _ratio(counts["clean_false_alarms"], counts["clean_cases"]),
    }


def score_report(report, labels, annotations=None):
    report = to_data(report)
    labels = Labels.model_validate(labels)
    if any(report.get("source_hashes", {}).get(path) != value for path, value in labels.source_hashes.items()):
        raise ValueError("Human labels do not match the source version reviewed by this run")
    if report.get("status") not in {"completed", "proposed"}:
        return {"run_id": report.get("run_id"), "case_id": labels.case_id, "eligible": False,
                "reason": "Run did not complete successfully; no quality score was assigned", "metrics": None}
    predictions = [Prediction.model_validate(item) for item in report.get("findings", [])]
    predictions = [item for item in predictions if item.status == "confirmed"]
    if len({item.id for item in predictions}) != len(predictions):
        raise ValueError("Prediction IDs must be unique")
    expected = {item.id: item for item in labels.expected}
    by_id = {item.id: item for item in predictions}
    matches = {}
    repairs = []

    def located(prediction, target):
        return (target.line_start is not None and prediction.line is not None
                and target.line_start-labels.line_tolerance <= prediction.line
                <= (target.line_end or target.line_start)+labels.line_tolerance)

    if annotations is not None:
        annotations = HumanAnnotations.model_validate(annotations)
        if annotations.run_id != report.get("run_id") or annotations.result_hash != digest(report):
            raise ValueError("Human annotations do not belong to this run/result")
        if (len(annotations.findings) != len(by_id) or {item.finding_id for item in annotations.findings} != set(by_id)
                or not all(item.reviewed for item in annotations.findings)):
            raise ValueError("Human review must cover every confirmed finding exactly once")
        for item in annotations.findings:
            if item.expected_id is not None:
                if item.expected_id not in expected or item.expected_id in matches.values():
                    raise ValueError("Invalid or duplicate human match")
                if safe_path(by_id[item.finding_id].file_path) != expected[item.expected_id].file_path:
                    raise ValueError("Human match refers to a different file")
                matches[item.finding_id] = item.expected_id
        repairs = annotations.repairs
    else:
        # First maximize location-correct matches, then match remaining category/file pairs.
        # Text descriptions, titles and verdict keywords never participate in scoring.
        owners = {}

        def compatible(prediction, target):
            return prediction.category == target.category and safe_path(prediction.file_path) == target.file_path

        def assign(prediction, seen):
            for target in labels.expected:
                if target.id in seen or not compatible(prediction, target) or not located(prediction, target):
                    continue
                seen.add(target.id)
                if target.id not in owners or assign(by_id[owners[target.id]], seen):
                    owners[target.id] = prediction.id
                    return True
            return False

        for prediction in predictions:
            assign(prediction, set())
        matches = {prediction_id: expected_id for expected_id, prediction_id in owners.items()}
        for prediction in predictions:
            if prediction.id in matches:
                continue
            for target in labels.expected:
                if target.id not in matches.values() and compatible(prediction, target):
                    matches[prediction.id] = target.id
                    break

    assessed_repairs = set()
    patch = report.get("patch") or {}
    patch_hash = hashlib.sha256(patch.get("diff", "").encode()).hexdigest()
    for repair in repairs:
        if repair.expected_id not in expected or not expected[repair.expected_id].fixable or repair.expected_id in assessed_repairs:
            raise ValueError("Repair assessment refers to an invalid or duplicate expected finding")
        if repair.patch_sha256 != patch_hash or not patch.get("diff"):
            raise ValueError("Repair assessment does not match the candidate patch")
        if repair.status == "passed" and (report.get("status") != "proposed" or repair.expected_id not in matches.values()):
            raise ValueError("A passed repair assessment requires a matched finding and a proposed patch")
        assessed_repairs.add(repair.expected_id)
    counts = {
        "true_positives": len(matches), "false_positives": len(predictions)-len(matches),
        "false_negatives": len(expected)-len(matches),
        "location_assessed": sum(expected[item].line_start is not None for item in matches.values()),
        "localized_matches": sum(located(by_id[prediction], expected[target]) for prediction, target in matches.items()),
        "expected_fixable": sum(item.fixable for item in labels.expected),
        "repair_assessed": len(repairs), "repair_passed": sum(item.status == "passed" for item in repairs),
        "clean_cases": int(not expected), "clean_false_alarms": int(not expected and bool(predictions)),
    }
    return {"run_id": report.get("run_id"), "run_dir": report.get("run_dir"), "result_hash": digest(report), "case_id": labels.case_id, "eligible": True,
            "assessment_source": "human_review" if annotations is not None else "category_and_file_against_human_labels",
            "metrics": metrics(counts), "matches": matches,
            "false_positive_ids": [item.id for item in predictions if item.id not in matches],
            "missed_expected_ids": [item for item in expected if item not in matches.values()],
            "unconfirmed_count": sum(item.get("status") == "candidate" for item in report.get("findings", [])),
            "repair_evidence": [item.model_dump() for item in repairs],
            "proposal_status": report.get("status"), "static_status": (report.get("validation") or {}).get("status", "not_run"),
            "model_regression_status": (report.get("regression") or {}).get("status", "not_run")}


def aggregate(results):
    eligible = [item for item in results if item["eligible"]]
    keys = ("true_positives", "false_positives", "false_negatives", "location_assessed", "localized_matches",
            "expected_fixable", "repair_assessed", "repair_passed", "clean_cases", "clean_false_alarms")
    return {"eligible_cases": len(eligible), "failed_cases": len(results)-len(eligible),
            "metrics": metrics({key: sum(item["metrics"][key] for item in eligible) for key in keys})}
