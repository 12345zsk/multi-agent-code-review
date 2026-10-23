"""Compatibility text API backed by the unified structured workflow."""

from src.workflow.service import run_workflow


def run_review(code_diff: str, file_paths: list[str] | None = None, **options) -> str:
    """Review a diff and return Markdown; accepts run_workflow keyword options."""
    return run_workflow(code_diff, file_paths, **options)["final_review"]


if __name__ == "__main__":
    from src.cli import main
    main()
