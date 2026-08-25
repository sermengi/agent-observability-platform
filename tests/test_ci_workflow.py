from pathlib import Path


def test_github_actions_workflow_runs_phase_0_checks_sequentially() -> None:
    workflow_path = Path(".github/workflows/ci.yml")

    assert workflow_path.exists()

    workflow = workflow_path.read_text()
    install_index = workflow.index("uv sync --frozen")
    lint_index = workflow.index("uv run ruff check .")
    format_index = workflow.index("uv run ruff format --check .")
    typecheck_index = workflow.index("uv run mypy src tests")
    test_index = workflow.index("uv run pytest")
    docker_build_index = workflow.index("docker build -t obs-platform-api:ci .")

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert workflow.count("runs-on: ubuntu-latest") == 1
    assert "postgres:" in workflow
    assert "image: postgres:16" in workflow
    assert "docker compose up" not in workflow
    assert (
        install_index
        < lint_index
        < format_index
        < typecheck_index
        < test_index
        < docker_build_index
    )
    assert "docker run -d" in workflow
    assert "obs-platform-api:ci" in workflow
    assert "curl --fail http://localhost:8000/health" in workflow
