from pathlib import Path


def test_readme_documents_phase_0_quick_start_and_health_smoke_call() -> None:
    readme = Path("README.md").read_text()

    assert "## Quick start" in readme
    assert "git clone" in readme
    assert "cp .env.example .env" in readme
    assert "make up" in readme
    assert "curl --fail http://localhost:8000/health" in readme
    assert '{"status":"ok","checks":{"database":"ok"}}' in readme


def test_readme_documents_host_native_local_development_loop() -> None:
    readme = Path("README.md").read_text()

    assert "## Local development" in readme
    assert "docker compose up -d --wait postgres" in readme
    assert "uv run uvicorn obs_platform.main:app --reload" in readme


def test_readme_stays_scoped_to_phase_0_startup() -> None:
    readme = Path("README.md").read_text().lower()

    excluded_topics = {
        "architecture diagram",
        "evaluation philosophy",
        "failure taxonomy",
        "project 1 relationship",
        "contributor guide",
    }

    for topic in excluded_topics:
        assert topic not in readme
