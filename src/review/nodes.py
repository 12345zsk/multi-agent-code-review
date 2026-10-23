"""Eight evidence-bounded specialists and a structured finding adjudicator."""

from dataclasses import asdict, replace
import hashlib

from src.contracts import AgentReport, AgentSpec, Finding, NodeHandler, WorkflowState
from src.diff import parse_diff, safe_path
from src.model_agent import ModelOutputError, invoke_json
from src.review.evidence import locate_evidence, supplied_evidence
from src.runtime.tracing import trace_stage
from src.schemas import AdjudicationReply, ReviewReply
from src.task_intent import task_context

_FOCUS = {
    "bug_detector": "Check control flow, boundary values, null handling, state mutation, exceptions and return values.",
    "security": "Check injection, authorization, unsafe deserialization, hardcoded credentials, data exposure and trust boundaries. Describe redacted credentials without reconstructing them.",
    "code_quality": "Report concrete maintainability costs, duplication and excessive complexity. Avoid subjective style preferences and trivial nitpicks.",
    "test_coverage": "Find specific changed branches or edge cases with no demonstrated tests. Do not assert measured coverage, and distinguish missing evidence from proven absence.",
    "architecture": "Check module responsibilities, dependency direction and coupling using only supplied context.",
    "performance": "Check algorithmic cost, resource leaks, unbounded work and concurrency. Do not invent benchmarks or assume hot paths or call frequency.",
    "dependency": "Check explicit dependency changes and constraints. Do not claim a CVE or current vulnerability without supplied evidence; no external lookup is available.",
    "compatibility": "Check visible API, schema, configuration and behavior changes for compatibility regressions.",
}


def make_specialist_node(spec: AgentSpec, model, *, react=True) -> NodeHandler:
    def review_node(state: WorkflowState) -> dict:
        changes = {change.path: change for change in parse_diff(state["code_diff"])}
        invoke, options = invoke_json, {}
        payload = {**task_context(state), "diff": state["code_diff"], "file_paths": state["file_paths"], "sources": state.get("file_contents", {}),
                   "target_lines": {path: change.target_lines() for path, change in changes.items()}}
        if react:
            from src.react import invoke_react_json
            invoke, options = invoke_react_json, {"sources": payload.pop("sources")}
            payload["available_sources"] = {path: {"total_lines": len(source.splitlines())}
                                            for path, source in options["sources"].items()}
        reply = invoke(
            model, spec.id,
            f"You review {spec.responsibility}. {_FOCUS[spec.id]} "
            "Report only actionable issues introduced by this diff. Use target-side line numbers. "
            "Evidence must be an exact nonempty excerpt from this file's supplied diff or source. "
            "Use an empty findings list when no issue is supported. Do not invent missing context. "
            "Respect declared types and documented contracts: invalid-type inputs are not defects unless a supplied caller or contract requires them. "
            "Do not claim repository-wide absence of tests from this limited context. "
            "Use critical only with demonstrated severe impact AND exposure; a vulnerability category alone is insufficient. "
            "Set category to the most specific schema category; use other only when no defined category fits.",
            payload, ReviewReply, **options,
        )
        findings = []
        seen = set()
        for item in reply.findings:
            path = safe_path(item.file_path)
            if path not in changes:
                raise ModelOutputError("Finding references an unchanged file", code="FINDING_PATH_INVALID", stage="finding_evidence")
            with trace_stage("finding_evidence", file_path=path):
                line = locate_evidence(changes[path], state.get("file_contents", {}).get(path), item.evidence, item.line)
            fingerprint = f"{spec.id}|{path}|{line}|{item.title.strip().casefold()}"
            finding_id = spec.id + "-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
            if finding_id not in seen:
                findings.append(Finding(
                    id=finding_id, agent_id=spec.id, **{**item.model_dump(), "file_path": path, "line": line}
                ))
                seen.add(finding_id)
        # Free-form model summaries have no per-claim evidence contract.
        summary = f"{len(findings)} candidate finding(s). Scope: supplied diff/source only; no repository-wide test or runtime checks."
        return {"reports": {spec.id: AgentReport(spec.id, "completed", tuple(findings), summary)}}

    return review_node


def adjudicator_node(state: WorkflowState, model) -> dict:
    candidates = [
        finding for agent in state["active_agents"]
        for finding in state.get("reports", {}).get(agent, AgentReport(agent)).findings
    ]
    if not candidates:
        return {"findings": []}
    reply = invoke_json(
        model, "adjudicator",
        "Evaluate every candidate against the supplied code. Return exactly one decision per ID. "
        "Confirm only evidence-supported introduced issues; keep uncertainty as candidate, reject false positives. "
        "Rewrite each title, description and suggestion to remove unsupported secondary claims, including invalid-type inputs outside declared contracts, "
        "assumed hot paths and repository-wide test absence. Reject a candidate if its core claim is unsupported. "
        "Assign severity independently of specialist votes and provide severity_reason: low for minor costs, medium for limited functional defects, "
        "high for substantial correctness/security impact. Critical requires demonstrated severe impact AND exposure/blast radius in supplied context; "
        "supply exact excerpts in critical_impact_evidence and critical_exposure_evidence, explaining their relevance in severity_reason. "
        "If context does not establish both, use at most high. Duplicate severity must not automatically raise canonical severity. "
        "For duplicates set duplicate_of to a canonical candidate ID whose own duplicate_of is null. "
        "Assign the canonical category from the schema taxonomy based on the actual defect, independently of the specialist. "
        "Do not invent IDs, code, findings or test results.",
        {**task_context(state), "diff": state["code_diff"], "sources": state.get("file_contents", {}), "candidates": [asdict(f) for f in candidates]},
        AdjudicationReply,
    )
    decisions = {item.finding_id: item for item in reply.decisions}
    by_id = {finding.id: finding for finding in candidates}
    if len(decisions) != len(reply.decisions) or set(decisions) != set(by_id):
        raise ModelOutputError("Adjudication must cover every candidate exactly once", code="ADJUDICATION_COVERAGE_INVALID", stage="adjudication_validation")
    severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    groups = {}
    for finding in candidates:
        decision = decisions[finding.id]
        canonical = decision.duplicate_of or finding.id
        if canonical not in by_id or (decision.duplicate_of and (
            canonical == finding.id or decisions[canonical].duplicate_of is not None
            or by_id[canonical].file_path != finding.file_path
        )):
            raise ModelOutputError("Invalid duplicate reference", code="ADJUDICATION_DUPLICATE_INVALID", stage="adjudication_validation")
        groups.setdefault(canonical, []).append(finding)
    findings = []
    changes = parse_diff(state["code_diff"])
    sources = state.get("file_contents", {})
    for canonical in groups:
        decision = decisions[canonical]
        severity = decision.severity
        severity_reason = decision.severity_reason
        if severity == "critical" and not all(supplied_evidence(changes, sources, excerpt) for excerpt in (
            decision.critical_impact_evidence, decision.critical_exposure_evidence,
        )):
            severity = "high"
            severity_reason = "Limited to high: critical impact and exposure evidence were not both supplied and verified. " + severity_reason
        original = by_id[canonical]
        findings.append(replace(
            original, severity=severity, status=decision.status,
            title=decision.title, category=decision.category, description=decision.description, suggestion=decision.suggestion,
            fixable=original.fixable and decision.status == "confirmed",
            adjudication=decision.reason, severity_reason=severity_reason,
            critical_impact_evidence=decision.critical_impact_evidence or "",
            critical_exposure_evidence=decision.critical_exposure_evidence or "",
        ))
    findings.sort(key=lambda finding: (-severity_order[finding.severity], finding.file_path, finding.line or 0, finding.id))
    return {"findings": findings}
