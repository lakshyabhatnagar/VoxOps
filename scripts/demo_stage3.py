"""Real Docker/API/repair demo; model and GitHub are simulated, Git stays local."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from demo_stage2 import FakeGitHub, LocalGit
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from voxops.agents.artifact_agent import INSTRUCTIONS
from voxops.artifacts.generator import ArtifactGenerator
from voxops.artifacts.service import ArtifactService
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactOutput
from voxops.github.publisher import GitPublisher
from voxops.main import create_app
from voxops.repository.snapshot import capture_source
from voxops.storage.artifact_store import ArtifactStore


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


# Run the disposable Stage 3 demonstration through a local HTTP server and real Docker.
# Use a local bare Git remote and simulated model and GitHub metadata rather than publishing a
# real project.
# Approve the final review, verify the resulting branch SHA, and check the original source and
# cleanup results.
def main():
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY"):
        os.environ.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="voxops-stage3-demo-") as temporary:
        directory = Path(temporary).resolve()
        source = directory / "source"
        source.mkdir()
        (source / "requirements.txt").write_text("fastapi==0.115.12\nuvicorn==0.34.3\n")
        (source / "main.py").write_text(
            "import os\nfrom fastapi import FastAPI\n"
            "TOKEN = os.environ['TOKEN']\nPORT = os.getenv('PORT', '8000')\n"
            "app = FastAPI()\n@app.get('/')\ndef root():\n    return {'ready': bool(TOKEN)}\n"
        )
        (source / "README.md").write_text(
            "# Disposable real Docker fixture\n\n```sh\n"
            "uvicorn main:app --host 0.0.0.0 --port 8000\n```\n"
        )
        before = capture_source(source)[0]
        remote = directory / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        settings = Settings(allowed_roots=[directory], data_dir=directory / "records")
        app = create_app(settings)
        app.state.worktrees.root = directory / "worktrees"
        store = ArtifactStore(settings.data_dir)
        connection = FakeGitHub(exists=False)
        publisher = GitPublisher(
            settings, store, app.state.worktrees, connection, LocalGit(settings, remote)
        )
        calls = []
        app.state.artifacts = ArtifactService(
            settings,
            app.state.store,
            app.state.worktrees,
            ArtifactGenerator(settings, store, fixture_agent(calls)),
            publisher,
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=1800
            ) as client:

                # Send one HTTP request to the demo's local VoxOps server.
                # Reject error responses and decode successful JSON responses.
                # Keep the demo steps readable while still exercising the actual API routes.
                def request(method, path, body=None, expected=200):
                    response = client.request(method, path, json=body)
                    assert response.status_code == expected, (response.status_code, response.text)
                    return response.json()

                analysis = request("POST", "/api/v1/repositories/analyze", {"path": str(source)})
                plan = request(
                    "POST",
                    "/api/v1/artifact-runs/plan",
                    {
                        "path": str(source),
                        "strategies": {analysis["detected_units"][0]["id"]: "container"},
                    },
                )
                if plan.get("unresolved_questions"):
                    raise AssertionError(plan["unresolved_questions"])
                run = request(
                    "POST",
                    "/api/v1/artifact-runs",
                    {
                        "plan_id": plan["plan_id"],
                        "owner": "example",
                        "name": "project",
                        "selected_paths": list(capture_source(source)[1]),
                    },
                    expected=201,
                )
                prefix = f"/api/v1/artifact-runs/{run['run_id']}"
                assert run["status"] == "awaiting_source_publication", run["errors"]
                baseline = request("GET", prefix + "/review")["review"]
                assert baseline["purpose"] == "baseline_publication"
                request(
                    "POST",
                    prefix + "/approvals",
                    {
                        "review_id": baseline["review_id"],
                        "digest": baseline["digest"],
                        "decision": "approve",
                    },
                )
                run = request("POST", prefix + "/execute")
                assert run["execution"]["status"] == "waiting_for_env", run
                form = request("GET", prefix + "/verification/env-form")
                fields = [
                    item
                    for item in form["schema"]["requirements"]
                    if item["necessity"] == "required" and item["resolution"] == "missing"
                ]
                assert [item["name"] for item in fields] == ["TOKEN"]
                request(
                    "POST",
                    prefix + "/verification/env-values",
                    {
                        "schema_digest": form["schema_digest"],
                        "values": {},
                    },
                    expected=422,
                )
                secret = "real-runtime-fixture-value"
                request(
                    "POST",
                    prefix + "/verification/env-values",
                    {
                        "schema_digest": form["schema_digest"],
                        "values": {fields[0]["field_id"]: secret},
                    },
                )
                print(
                    "START real loopback API and Docker repair journey", run["run_id"], flush=True
                )
                run = request("POST", prefix + "/execute")
                assert run["execution"]["status"] == "passed", run
                review = request("GET", prefix + "/review")["review"]
                assert review["purpose"] == "artifact_publication"
                assert review["review_id"] != baseline["review_id"]
                assert "APPLICATION_FILE=main.py" in review["diff"]
                assert review["execution_attestation"]["status"] == "passed"
                assert secret not in json.dumps(run) + json.dumps(review)
                docker_calls = [feedback for kind, feedback in calls if kind == "dockerfile"]
                assert len(docker_calls) == 2 and docker_calls[0] is None
                assert (
                    "checksum" in docker_calls[1].lower() or "not found" in docker_calls[1].lower()
                )
                request(
                    "POST",
                    prefix + "/approvals",
                    {
                        "review_id": review["review_id"],
                        "digest": review["digest"],
                        "decision": "approve",
                    },
                )
                published = request("POST", prefix + "/publish")
                result = request("GET", prefix + "/result")
                actual = subprocess.run(
                    ["git", "-C", str(remote), "rev-parse", f"refs/heads/{run['branch']}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                assert actual == published["result"]["commit_sha"]
                assert actual == result["result"]["remote_sha"]
                assert result["deployment_executed"] is False
                assert capture_source(source)[0] == before
                assert connection.creations == 1
                for file in settings.data_dir.glob("*.json"):
                    assert secret not in file.read_text()
                assert all(item["state"] == "removed" for item in run["execution"]["resources"])
                print(
                    json.dumps(
                        {
                            "scope": "real HTTP/Docker/local Git; simulated model and GitHub",
                            "run_id": run["run_id"],
                            "branch": run["branch"],
                            "remote_sha": actual,
                            "verification_status": result["result"]["verification_status"],
                            "content_calls_for_dockerfile": len(docker_calls),
                            "source_unchanged": True,
                            "deployment_executed": False,
                        },
                        indent=2,
                    ),
                    flush=True,
                )
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive(), "Disposable API server failed to stop"


if __name__ == "__main__":
    main()
