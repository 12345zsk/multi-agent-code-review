"""Specialist catalog shared by routing, graph construction, and reporting."""

from src.contracts import AgentSpec

REVIEW_AGENTS = (
    AgentSpec("bug_detector", "Logic, correctness, and boundary conditions"),
    AgentSpec("security", "Security vulnerabilities and trust boundaries"),
    AgentSpec("code_quality", "Readability and maintainability"),
    AgentSpec("test_coverage", "Missing tests and regression risks"),
    AgentSpec("architecture", "Module boundaries and dependency direction"),
    AgentSpec("performance", "Hot paths, resource usage, and concurrency"),
    AgentSpec("dependency", "Dependency changes, versions, and supply-chain risks"),
    AgentSpec("compatibility", "API, schema, and backward compatibility"),
)

AGENT_IDS = tuple(agent.id for agent in REVIEW_AGENTS)
