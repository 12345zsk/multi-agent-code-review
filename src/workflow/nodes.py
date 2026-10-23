"""Deterministic context preparation, explicit routing, and Markdown reporting."""

from src.contracts import WorkflowState


def context_collector_node(state: WorkflowState) -> dict:
    """Organize caller-supplied context without reading any repository files."""
    import json
    from src.diff import parse_diff

    changes = parse_diff(state["code_diff"])
    return {"context": {
        "diff": state["code_diff"],
        "files": json.dumps([
            {"path": change.path, "hunks": len(change.hunks), "target_lines": sorted(change.target_lines())}
            for change in changes
        ]),
    }}


def review_planner_node(state: WorkflowState) -> dict:
    """Keep the explicit selection; the service defaults to all specialists."""
    return {"active_agents": state["active_agents"]}


def reporter_node(state: WorkflowState) -> dict:
    """Render useful findings and proposed patches without overstating validation."""
    from src.privacy import redact

    intent = state.get("task_intent", {})
    if intent.get("status") in {"unsupported", "needs_clarification"}:
        title = "无法完成该任务" if intent["status"] == "unsupported" else "需要澄清任务"
        lines = ["# " + title, "Status: " + intent["status"],
                 "Run ID: " + state.get("run_id", "untracked"), "", intent["reason"]]
        if intent.get("unsupported_requests"):
            lines.extend(["", "不支持的要求：", *["- " + item for item in intent["unsupported_requests"]]])
        lines.extend(["", "本次未执行代码审查、修正或文件写入。",
                      "当前支持：审查提供的 diff 或显式指定的源码文件、修正确认的缺陷、生成候选回归测试及静态/模型验证。",
                      "自然语言明确要求修正本地文件时，可校验、备份后写回；不支持运行目标测试、联网、部署或新功能开发。"])
        return {"status": intent["status"], "final_review": redact("\n".join(lines))}

    reports = state.get("reports", {})
    findings = state.get("findings", [])
    if state.get("errors") and not findings:
        findings = [finding for report in reports.values() for finding in report.findings]
    pending = sorted(set(state.get("pending_steps", [])))
    incomplete = bool(pending) or not state["active_agents"] or any(
        agent not in reports or reports[agent].status != "completed"
        for agent in state["active_agents"]
    )
    failed = bool(state.get("errors")) or any(report.status == "failed" for report in reports.values())
    actionable = [f for f in findings if f.status == "confirmed" and f.fixable]
    patch = state.get("patch")
    tests = state.get("test_patch")
    validation = state.get("validation")
    regression = state.get("regression")
    proposing = state["mode"] == "fix" and bool(actionable)
    if proposing:
        failed = failed or any(result and result.status == "failed" for result in (validation, regression))
        incomplete = incomplete or not (
            patch and patch.status == "generated" and tests and tests.status in {"generated", "not_needed"}
            and validation and (validation.status == "passed" or (validation.status == "not_run" and validation.summary))
            and regression and regression.status == "passed"
        )
        if state.get("write_root"):
            incomplete = incomplete or state.get("write_result", {}).get("status") != "written"
    status = "failed" if failed else "incomplete" if incomplete else "proposed" if proposing else "completed"
    confirmed = [f for f in findings if f.status == "confirmed"]
    uncertain = [f for f in findings if f.status == "candidate"]
    if status in {"failed", "incomplete"} or uncertain:
        verdict = "NEEDS DISCUSSION"
    elif any(f.severity in {"critical", "high", "medium"} for f in confirmed):
        verdict = "REQUEST CHANGES"
    else:
        verdict = "APPROVE"
    lines = [
        "# Code Review", f"Mode: {state['mode']} | Status: {status}",
        f"Verdict on input {'source file' if state.get('review_scope') == 'file' else 'diff'}: {verdict}",
        f"Run ID: {state.get('run_id', 'untracked')} | Model: {state.get('model_name', 'unknown')}",
        "", "## Review coverage",
    ]
    if state.get("react"):
        lines.insert(4, "Review strategy: ReAct | Tool: read_source (input snapshot only)")
    if state.get("review_scope") == "file":
        lines.insert(4, "Review scope: full source file | Target: " + ", ".join(state["file_paths"]))
    if intent.get("status") == "accepted":
        lines.extend(["## Task guidance", intent["guidance"], ""])
    elif state.get("user_task") and state.get("errors"):
        lines.extend(["自然语言任务解析失败，本次未开始代码审查、修正或文件写入。", ""])
    for agent in state["active_agents"]:
        report = reports.get(agent)
        lines.append(f"- {agent}: {report.status if report else 'missing'} — {report.summary if report else ''}")
    lines.extend(["", "## Findings"])
    for finding in findings:
        if finding.status == "rejected":
            continue
        lines.extend([
            f"- **[{finding.severity}/{finding.status}] {finding.title}** ({finding.file_path}:{finding.line or '?'}) [{finding.id}]",
            f"  Category: {finding.category}",
            f"  {finding.description}", f"  Suggested fix: {finding.suggestion or 'Manual review required.'}",
            f"  Evidence: {finding.evidence}", f"  Adjudication: {finding.adjudication}",
        ])
        if finding.severity_reason:
            lines.append(f"  Severity basis: {finding.severity_reason}")
        if finding.severity == "critical":
            lines.extend([
                f"  Critical impact evidence: {finding.critical_impact_evidence}",
                f"  Critical exposure evidence: {finding.critical_exposure_evidence}",
            ])
    if not any(f.status != "rejected" for f in findings):
        lines.append("No supported issues found in the selected review scope." if status == "completed" else "No confirmed result; review is incomplete.")
    lines.extend(["", f"Fix attempts: {state['fix_attempts']}"])
    if state.get("fix_plan"):
        lines.extend(["", "## Repair plan", state["fix_plan"].description])
    publishable = (status == "proposed" and validation and validation.status == "passed"
                   and regression and regression.status == "passed")
    prefix = "Candidate" if publishable else "Draft"
    for heading, artifact in ((prefix+" code patch", patch), (prefix+" test patch", tests)):
        if artifact:
            lines.extend(["", "## " + heading, artifact.summary])
            lines.append("Artifact classification: " + ("validated_candidate" if publishable else "draft; not approved for publication"))
            if artifact.diff:
                # Indentation keeps arbitrary code-fence content inside a code block.
                lines.append("")
                lines.extend("    " + line for line in artifact.diff.splitlines())
    lines.extend(["", "## Validation"])
    for name in ("validation", "regression", "runtime_validation"):
        result = state.get(name)
        lines.append(f"- {name}: {result.status if result else 'not_run'} ({result.scope if result else 'unknown'})")
        if result:
            lines.append("  " + result.summary)
            if result.checks:
                lines.append("  Checks: " + ", ".join(result.checks))
    if patch:
        lines.append("Runtime tests were not executed; see local file writing status below." if state.get("write_root")
                     else "Candidate patch only: repository files were not changed and runtime tests were not executed.")
    if state.get("write_root"):
        result = state.get("write_result", {})
        lines.extend(["", "## Local file writing", "Status: " + result.get("status", "not_run")])
        for item in result.get("files", []):
            lines.append(f"- {item['file_path']}: {item['status']} | backup: {item.get('backup') or 'new file; no previous content'}")
        lines.append("Backups and per-file write receipts: " + state.get("run_dir", "") + "/writes/")
        if status == "completed" and not actionable and not failed:
            lines.append("No confirmed fixable findings; local files were not changed.")
        elif result.get("status") != "written":
            lines.append("Interrupted writes may be partial; inspect per-file receipts before editing or resuming.")
    if pending:
        lines.extend(["", "Pending implementations: " + ", ".join(pending)])
    if state.get("errors"):
        lines.extend(["", "## Diagnostics", *["- " + error for error in state["errors"]]])
    if state.get("diagnostics"):
        lines.extend(["", "## Execution trace"])
        for item in state["diagnostics"]:
            label = "resolved" if item.get("resolved") else "unresolved"
            location = f" {item['file_path']}:{item.get('line') or '?'}" if item.get("file_path") else ""
            lines.append(f"- {item['node']} / {item.get('stage', 'execution')} / attempt {item.get('attempt', 0)}: "
                         f"{item.get('error_code', 'UNKNOWN')} ({label}){location} [event {item['event_id']}]")
        if state.get("run_dir"):
            lines.append("Details: " + state["run_dir"] + "/events.jsonl and diagnostics.json")
    return {"status": status, "final_review": redact("\n".join(lines))}
