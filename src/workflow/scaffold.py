"""Explicit offline scaffold mode. Production mode never uses these handlers."""

from src.contracts import AgentReport, FixPlan, PatchArtifact, ValidationResult
from src.workflow.registry import AGENT_IDS


def scaffold_handlers() -> dict:
    handlers = {
        agent: (lambda state, name=agent: {
            "reports": {name: AgentReport(name, summary="Offline scaffold; model review not requested.")},
            "pending_steps": [name],
        }) for agent in AGENT_IDS
    }
    handlers.update({
        "review_planner": lambda state: {"active_agents": [], "review_plan": [], "pending_steps": ["review_planner"]},
        "adjudicator": lambda state: {"findings": [], "pending_steps": ["adjudicator"]},
        "fix_planner": lambda state: {"fix_plan": FixPlan(), "pending_steps": ["fix_planner"]},
        "patch_generator": lambda state: {"patch": PatchArtifact(), "pending_steps": ["patch_generator"]},
        "test_generator": lambda state: {"test_patch": PatchArtifact(), "pending_steps": ["test_generator"]},
        "validator": lambda state: {"validation": ValidationResult(), "pending_steps": ["validator"]},
        "regression_reviewer": lambda state: {"regression": ValidationResult(scope="model"), "pending_steps": ["regression_reviewer"]},
        "failure_diagnoser": lambda state: {"failure_diagnosis": {"action": "stop"}, "pending_steps": ["failure_diagnoser"]},
    })
    return handlers
