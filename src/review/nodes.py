"""Eight evidence-bounded specialists and a structured finding adjudicator."""

from dataclasses import asdict, replace
import hashlib

from src.contracts import AgentReport, AgentSpec, Finding, NodeHandler, WorkflowState
from src.diff import parse_diff, safe_path
from src.model_agent import ModelOutputError, invoke_json
from src.review.evidence import locate_evidence, supplied_evidence
from src.runtime.tracing import trace_stage
from src.schemas import AdjudicationReply, DisputeReply, ReviewReply
from src.project_tools import available_sources, react_options
from src.task_intent import task_context

_FOCUS = {
    "bug_detector": "Check control flow, boundary values, null handling, state mutation, exceptions and return values.",
    "security": "Check injection, authorization, unsafe deserialization, hardcoded credentials, data exposure and trust boundaries. Describe redacted credentials without reconstructing them.",
    "code_quality": "Report concrete maintainability costs, duplication and excessive complexity. Avoid subjective style preferences and trivial nitpicks.",
    "test_coverage": "Find specific branches or edge cases within the review scope with no demonstrated tests. Do not assert measured coverage, and distinguish missing evidence from proven absence.",
    "architecture": "Check module responsibilities, dependency direction and coupling using only supplied context.",
    "performance": "Check algorithmic cost, resource leaks, unbounded work and concurrency. Do not invent benchmarks or assume hot paths or call frequency.",
    "dependency": "Check explicit dependency changes and constraints. Do not claim a CVE or current vulnerability without supplied evidence; no external lookup is available.",
    "compatibility": "Check visible API, schema, configuration and behavior changes for compatibility regressions.",
}

_CLAIM_GUIDANCE = (
    "State the input conditions under which each claimed result or exception occurs. Trace earlier calls "
    "and exceptions before claiming a later failure. Do not say 'every input' or 'all calls' without "
    "checking all documented input branches. For example, if an exporter first calls an average helper "
    "that divides by len(items), then accesses a missing summary key, empty input raises ZeroDivisionError "
    "before the key access; KeyError applies to nonempty valid input. Describe these cases separately. "
    "A truncated search, unavailable files, a path inventory, or zero text matches cannot prove absent tests "
    "or measured coverage. Preserve tests.missing_coverage as candidate; no authoritative test-coverage "
    "results are provided by this workflow. Do not invent test absence or mark such observations auto-fixable. "
)


def _coverage_observation(finding, category=None):
    return (finding.category == "tests.missing_coverage" or category == "tests.missing_coverage"
            or (finding.agent_id == "test_coverage" and (category or finding.category) == "other"))


def _unverified_coverage(finding):
    """Static code excerpts and searches cannot certify negative coverage claims."""
    return replace(
        finding, category="tests.missing_coverage", status="candidate", fixable=False,
        title="Regression-test evidence remains unverified",
        description="Available static evidence does not establish whether the behavior in the cited code is tested. "
                    "Truncated searches, unreadable files and zero text matches cannot prove that tests are absent "
                    "or establish measured coverage. Verify the cited contract and proposed regression cases before adding tests.",
        adjudication="Kept as candidate: no authoritative test-coverage evidence was provided; static searches "
                     "and matching source excerpts cannot confirm missing tests.",
        severity_reason="Unverified coverage observation; this rating does not establish that tests are absent.",
    )


def make_specialist_node(spec: AgentSpec, model, *, react=True) -> NodeHandler:
    def review_node(state: WorkflowState) -> dict:
        whole_file = state.get("review_scope") == "file"
        changes = {change.path: change for change in parse_diff(state["code_diff"])}
        invoke, options = invoke_json, {}
        payload = {**task_context(state), "diff": state["code_diff"], "file_paths": state["file_paths"], "sources": state.get("file_contents", {}),
                   "assignment": next((a for a in state.get("review_plan", []) if a["agent_id"] == spec.id), {}),
                   "project_files": list(state.get("project_inventory", {}))[:200],
                   "project_inventory_truncated": len(state.get("project_inventory", {})) > 200,
                   "target_lines": {path: change.target_lines() for path, change in changes.items()}}
        observed, observations = {}, []
        if react:
            from src.react import invoke_react_json
            payload.pop("sources")
            invoke, options = invoke_react_json, react_options(state, observed, tool_observations=observations)
            payload["available_sources"] = {path: {"total_lines": len(source.splitlines())}
                                            for path, source in options["sources"].items()}
        reply = invoke(
            model, spec.id,
            f"You review {spec.responsibility}. {_FOCUS[spec.id]} "
            + ("Review actionable existing defects in the complete current file, including syntax errors. "
               "The context-only diff encodes an existing file snapshot, not a change or a new file. "
               if whole_file else "Report only actionable issues introduced by this diff. ")
            + "Use target-side line numbers. "
            "Use your assigned review questions. Project tools may inspect related callers, configurations and tests, "
            "but report defects only in file_paths; context access does not expand write or finding scope. "
            "Evidence must be an exact nonempty excerpt from this file's supplied diff or source. "
            "Use an empty findings list when no issue is supported. Do not invent missing context. "
            "Respect declared types and documented contracts: invalid-type inputs are not defects unless a supplied caller or contract requires them. "
            "Do not claim repository-wide absence of tests from this limited context. "
            "Use critical only with demonstrated severe impact AND exposure; a vulnerability category alone is insufficient. "
            "Set category to the most specific schema category; use other only when no defined category fits. "
            + _CLAIM_GUIDANCE,
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
                finding = Finding(
                    id=finding_id, agent_id=spec.id, **{**item.model_dump(), "file_path": path, "line": line}
                )
                findings.append(_unverified_coverage(finding) if _coverage_observation(finding) else finding)
                seen.add(finding_id)
        # Free-form model summaries have no per-claim evidence contract.
        scope = "complete supplied file" if whole_file else "supplied diff/source only"
        summary = f"{len(findings)} candidate finding(s). Scope: {scope}; no repository-wide test or runtime checks."
        return {"reports": {spec.id: AgentReport(spec.id, "completed", tuple(findings), summary, tuple(observations))},
                "observed_sources": {spec.id: observed}}

    return review_node


def adjudicator_node(state: WorkflowState, model) -> dict:
    candidates = [
        finding for agent in state["active_agents"]
        for finding in state.get("reports", {}).get(agent, AgentReport(agent)).findings
    ]
    if not candidates:
        return {"findings": [], "review_disputes": []}
    reply = invoke_json(
        model, "adjudicator",
        "Evaluate every candidate against the supplied code. Return exactly one decision per ID. "
        + ("For file scope, confirm evidence-supported existing defects in the supplied current file, "
           "including syntax errors; the context-only diff is not a change comparison. "
           if state.get("review_scope") == "file" else "Confirm only evidence-supported introduced issues. ")
        + "Keep uncertainty as candidate, reject false positives. "
        "Rewrite each title, description and suggestion to remove unsupported secondary claims, including invalid-type inputs outside declared contracts, "
        "assumed hot paths and repository-wide test absence. Reject a candidate if its core claim is unsupported. "
        "Assign severity independently of specialist votes and provide severity_reason: low for minor costs, medium for limited functional defects, "
        "high for substantial correctness/security impact. Critical requires demonstrated severe impact AND exposure/blast radius in supplied context; "
        "supply exact excerpts in critical_impact_evidence and critical_exposure_evidence, explaining their relevance in severity_reason. "
        "If context does not establish both, use at most high. Duplicate severity must not automatically raise canonical severity. "
        "For duplicates set duplicate_of to a canonical candidate ID whose own duplicate_of is null. "
        "Assign the canonical category from the schema taxonomy based on the actual defect, independently of the specialist. "
        "For evidence conflicts or unresolved material uncertainty, request up to three targeted disputes: "
        "assign a registered specialist, existing finding IDs and one concrete missing-evidence question. "
        "Do not request review for mere agreement or duplicates. After the one permitted review round, "
        "use dispute_reviews as evidence, preserve uncertainty as candidate and return disputes=[]. "
        "Do not invent IDs, code, findings or test results. Read tool_observations and dispute tool_observations: "
        "status=error, truncated=true or nonempty unavailable explicitly limit the search evidence. "
        + _CLAIM_GUIDANCE,
        {**task_context(state), "diff": state["code_diff"], "sources": available_sources(state),
         "review_round": state.get("review_round", 0), "dispute_reviews": state.get("dispute_reviews", []),
         "specialists": list(_FOCUS), "candidates": [asdict(f) for f in candidates],
         "tool_observations": {agent: list(report.tool_observations)
                               for agent, report in state.get("reports", {}).items()}},
        AdjudicationReply,
    )
    decisions = {item.finding_id: item for item in reply.decisions}
    by_id = {finding.id: finding for finding in candidates}
    if len(decisions) != len(reply.decisions) or set(decisions) != set(by_id):
        raise ModelOutputError("Adjudication must cover every candidate exactly once", code="ADJUDICATION_COVERAGE_INVALID", stage="adjudication_validation")
    if any(len(set(d.finding_ids)) != len(d.finding_ids) or set(d.finding_ids)-set(by_id) for d in reply.disputes):
        raise ModelOutputError("Disputes must reference existing candidate IDs", code="DISPUTE_REFERENCE_INVALID", stage="adjudication_validation")
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
    sources = available_sources(state)
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
        finding = replace(
            original, severity=severity, status=decision.status,
            title=decision.title, category=decision.category, description=decision.description, suggestion=decision.suggestion,
            fixable=original.fixable and decision.status == "confirmed",
            adjudication=decision.reason, severity_reason=severity_reason,
            critical_impact_evidence=decision.critical_impact_evidence or "",
            critical_exposure_evidence=decision.critical_exposure_evidence or "",
        )
        if decision.status != "rejected" and _coverage_observation(original, decision.category):
            finding = _unverified_coverage(finding)
        findings.append(finding)
    findings.sort(key=lambda finding: (-severity_order[finding.severity], finding.file_path, finding.line or 0, finding.id))
    if state.get("review_round", 0) >= 1 and reply.disputes:
        unresolved = {decisions[fid].duplicate_of or fid for dispute in reply.disputes for fid in dispute.finding_ids}
        findings = [replace(f, status="candidate", fixable=False,
                            adjudication="Dispute budget exhausted; requires human clarification. " + f.adjudication)
                    if f.id in unresolved else f for f in findings]
    disputes = [d.model_dump() for d in reply.disputes]
    if not disputes and not state.get("review_round", 0):
        disputes = [{"agent_id": f.agent_id, "finding_ids": [f.id],
                     "question": "Resolve this uncertain finding with source/contract evidence: " + f.adjudication[:800]}
                    for f in findings if f.status == "candidate"][:3]
    return {"findings": findings, "review_disputes": disputes}


def dispute_reviewer_node(state: WorkflowState, model) -> dict:
    """Ask only assigned experts to resolve explicit disputes, for one bounded round."""
    from src.react import invoke_react_json
    candidates = {f.id: f for report in state.get("reports", {}).values() for f in report.findings}
    results, observed = [], {}
    for assignment in state.get("review_disputes", [])[:3]:
        findings = [candidates[fid] for fid in assignment["finding_ids"]]
        observations = []
        reply = invoke_react_json(model, "dispute_reviewer",
            f"Act as the assigned {assignment['agent_id']} specialist. {_FOCUS[assignment['agent_id']]} "
            "Resolve only the assigned dispute using exact source evidence and project tools. "
            "Return one resolution per supplied finding ID. Confirm only with a nonempty exact excerpt "
            "from sources you have actually observed. Preserve insufficient evidence as candidate. "
            "Do not use majority vote, invent findings, change files or claim test execution. " + _CLAIM_GUIDANCE,
            {**task_context(state), "assignment": assignment, "candidates": [asdict(f) for f in findings],
             "current_decisions": [asdict(f) for f in state.get("findings", [])],
             "tool_observations": {agent: list(report.tool_observations)
                                   for agent, report in state.get("reports", {}).items()}}, DisputeReply,
            **react_options({**state, "observed_sources": {**state.get("observed_sources", {}), "dispute_reviewer": observed}},
                            observed, tool_observations=observations))
        if len(reply.resolutions) != len(findings) or {r.finding_id for r in reply.resolutions} != set(assignment["finding_ids"]):
            raise ModelOutputError("Dispute coverage mismatch", code="DISPUTE_COVERAGE_INVALID", stage="dispute_validation")
        sources = {**available_sources(state), **observed}
        for resolution in reply.resolutions:
            finding = candidates[resolution.finding_id]
            if resolution.status == "confirmed" and _coverage_observation(finding):
                resolution = resolution.model_copy(update={
                    "status": "candidate", "reason": _unverified_coverage(finding).adjudication,
                })
            if resolution.status == "confirmed" and not supplied_evidence(parse_diff(state["code_diff"]), sources, resolution.evidence):
                raise ModelOutputError("Unverified dispute evidence", code="DISPUTE_EVIDENCE_INVALID", stage="dispute_validation")
            results.append({"agent_id": assignment["agent_id"], **resolution.model_dump(),
                            "tool_observations": list(observations)})
    return {"dispute_reviews": results, "review_round": state.get("review_round", 0)+1,
            "observed_sources": {"dispute_reviewer": observed}}
