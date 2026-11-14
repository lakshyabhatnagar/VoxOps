"""Real Docker/API/repair demo; model and GitHub are simulated, Git stays local."""

import json

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from voxops.agents.artifact_agent import INSTRUCTIONS
from voxops.domain.artifact_models import ArtifactOutput


# Create the demo's local simulated model rather than calling a paid model service.
# Generate a deliberately broken first Dockerfile and a corrected version after execution
# feedback.
# Record calls so the demo can show that an actual repair request occurred.
def fixture_agent(calls):
    # Read the generator's request and return the supported file content for that job.
    # Use the missing application path before execution feedback and the real path afterward.
    # This controls the model response while the demo still exercises the real HTTP, Docker,
    # repair, and Git flow.
    async def respond(messages, info):
        packet = json.loads(messages[-1].parts[0].content)
        task = packet["task"]
        kind = task["artifact_type"]
        calls.append((kind, packet.get("execution_feedback")))
        ignore = ".env\n.env.*\n.git/\n*.pem\n*.key\nnode_modules/\n.venv/\n"
        repaired = bool(packet.get("execution_feedback"))
        application = "main.py" if repaired else "missing_application.py"
        docker = (
            "FROM python:3.12-slim\nWORKDIR /app\n"
            f"ARG APPLICATION_FILE={application}\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "COPY ${APPLICATION_FILE} main.py\n"
            "USER 1000\nEXPOSE 8000\n"
            'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
        )
        steps = "".join(f"      - run: {command}\n" for command in task["expected_commands"])
        ci = (
            "name: build\non: [push, pull_request]\npermissions:\n  contents: read\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
            "        with:\n          persist-credentials: false\n" + steps
        )
        content = {"dockerfile": docker, "dockerignore": ignore, "gitignore": ignore, "ci": ci}[
            kind
        ]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"target_path": task["target_path"], "content": content},
                )
            ]
        )

    return Agent(
        FunctionModel(respond),
        output_type=ArtifactOutput,
        retries=0,
        instructions=INSTRUCTIONS,
        instrument=False,
    )
