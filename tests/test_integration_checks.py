import asyncio

import pytest
from check_integrations import check_model
from conftest import settings_for
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from voxops.domain.artifact_models import ArtifactOutput


@pytest.mark.parametrize("response", ["valid", "invalid", "missing_key"])
# Run the model-check command's real temporary project, scanner, planner, and file checks.
# Supply a local model response, or no key, so the test never contacts a provider.
# Require success only for a Dockerfile that passes validation and report failures honestly.
def test_model_check_requires_a_valid_generated_file(tmp_path, monkeypatch, response):
    settings = settings_for(tmp_path, model="nvidia:mistralai/mistral-large-4-1t-a52b")
    if response != "missing_key":
        content = (
            'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n'
            'USER 1000\nCMD ["uvicorn", "main:app"]\n'
            if response == "valid"
            else "This is not a Dockerfile.\n"
        )
        agent = Agent(
            TestModel(custom_output_args={"target_path": "Dockerfile", "content": content}),
            output_type=ArtifactOutput,
            retries=0,
        )
        monkeypatch.setattr("voxops.artifacts.generator.create_artifact_agent", lambda _: agent)

    result = asyncio.run(check_model(settings))

    if response == "valid":
        assert result["status"] == "passed"
        assert result["requests"] == result["attempts"] == 1
        assert any(item["status"] == "passed" for item in result["validations"])
        assert all(
            item["status"] in {"passed", "unavailable"} for item in result["validations"]
        )
    else:
        assert result["status"] == "failed"
        assert result["error_category"] == (
            "model_unavailable" if response == "missing_key" else "validation_exhausted"
        )
        assert result["attempts"] == (0 if response == "missing_key" else 3)
