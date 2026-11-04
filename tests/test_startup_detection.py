import json

import pytest
from conftest import settings_for

from voxops.frameworks.registry import detect_units
from voxops.repository.filesystem_scanner import scan_repository


@pytest.fixture
# Why: Reproduce AEREO's exported application and factory without running project code.
# How: Create a small Python package whose documented startup differs from factory inference.
def application(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "0.1.0"\ndependencies = ["fastapi", "uvicorn"]\n'
    )
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "# Why: Supply a known factory for startup detection.\n"
        "# How: Return an application and also export a module-level instance.\n"
        "def create_app():\n    return FastAPI()\n\napp = create_app()\n"
        "raise RuntimeError('scanner must not execute this module')\n"
    )
    return tmp_path


# Why: Prefer AEREO's documented command and retain its migration prerequisite.
# How: Scan README and Docker alternatives, then assert the local README command wins.
def test_documented_local_startup_and_migration(application):
    (application / "README.md").write_text(
        "# Example\n## Run locally\n```bash\nuv sync --locked --python 3.12\n"
        "cp .env.example .env\nuv run alembic upgrade head\n"
        "uv run uvicorn app.main:app --workers 1\n```\n"
    )
    (application / "Dockerfile").write_text(
        'FROM python:3.12\nCMD ["sh", "-c", "alembic upgrade head && exec '
        'uvicorn app.main:app --host 0.0.0.0 --port \\"${PORT:-10000}\\" --workers 1"]\n'
    )
    scan = scan_repository(application, settings_for(application))
    unit = detect_units(scan)[0]
    assert unit.runtime.start_command == "uv run uvicorn app.main:app --workers 1"
    assert unit.runtime.prerequisite_commands == ["uv run alembic upgrade head"]
    assert unit.runtime.command_source == "explicit"
    assert any(
        item.kind == "start_command" and item.path == "README.md"
        for item in unit.detection.evidence
    )
    assert "scanner must not execute" not in scan.model_dump_json()


# Why: Recognize an exported factory result and preserve the existing Procfile precedence.
# How: Compare source-only inference with a Procfile command alongside a different README command.
def test_exported_app_and_procfile_precedence(application):
    settings = settings_for(application)
    unit = detect_units(scan_repository(application, settings))[0]
    assert unit.runtime.start_command == "uvicorn app.main:app"
    (application / "README.md").write_text(
        "```bash\nuv run alembic upgrade head\nuv run uvicorn app.main:app --workers 1\n```\n"
    )
    (application / "Procfile").write_text("web: uvicorn app.main:app --workers 2\n")
    unit = detect_units(scan_repository(application, settings))[0]
    assert unit.runtime.start_command == "uvicorn app.main:app --workers 2"
    assert unit.runtime.prerequisite_commands == []


@pytest.mark.parametrize("shell", [False, True])
# Why: Read Docker startup metadata without confusing build instructions with runtime commands.
# How: Test final-stage exec and shell CMD forms, including a migration and PORT placeholder.
def test_docker_startup(application, shell):
    command = 'uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 1'
    payload = (
        ["sh", "-c", f"alembic upgrade head && exec {command}"]
        if shell
        else ["uvicorn", "app.main:app", "--workers", "1"]
    )
    (application / "Dockerfile").write_text(
        'FROM python:3.12 AS build\nCMD ["uvicorn", "wrong:app"]\n'
        "FROM python:3.12\nRUN echo not-a-start-command\nCMD " + json.dumps(payload) + "\n"
    )
    unit = detect_units(scan_repository(application, settings_for(application)))[0]
    assert unit.runtime.command_source == "explicit"
    assert unit.runtime.start_command == (command if shell else "uvicorn app.main:app --workers 1")
    assert unit.runtime.prerequisite_commands == (["alembic upgrade head"] if shell else [])


# Why: Keep README commands scoped to proven targets and exclude credentials and shell side effects.
# How: Mix unrelated examples and unsafe commands with a valid fenced startup block.
def test_startup_redaction_and_scope(application):
    marker = application / "executed"
    secret = "sensitive-startup-sentinel"
    (application / "README.md").write_text(
        f"Credentials: {secret}\n```bash\nuvicorn unrelated.main:app --workers 9\n```\n"
        f"```bash\nTOKEN={secret} uvicorn app.main:app\n"
        f"touch {marker} && uvicorn app.main:app\n```\n"
        "```bash\nuv run uvicorn app.main:app --workers 1\n```\n"
    )
    scan = scan_repository(application, settings_for(application))
    unit = detect_units(scan)[0]
    assert unit.runtime.start_command == "uv run uvicorn app.main:app --workers 1"
    assert secret not in scan.model_dump_json() + unit.model_dump_json()
    assert not marker.exists()
