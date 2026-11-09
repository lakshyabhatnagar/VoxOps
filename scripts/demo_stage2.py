"""Offline Stage 2 demo. All Git writes stay inside a newly created disposable directory."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from simulated_docker import DemoDocker

from voxops.agents.artifact_agent import INSTRUCTIONS
from voxops.artifacts.generator import ArtifactGenerator
from voxops.artifacts.service import ArtifactService
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactOutput, RepositoryTarget
from voxops.github.publisher import GitPublisher
from voxops.main import create_app
from voxops.repository.git_executor import GitExecutor
from voxops.repository.snapshot import capture_source
from voxops.storage.artifact_store import ArtifactStore


# Runs Git commands for the example without sending any files to GitHub.
# Redirects the example GitHub address to a temporary Git repository on this computer.
class LocalGit(GitExecutor):
    # Remembers the local Git repository where this example is allowed to push.
    # Keeps its path alongside the normal Git settings used by VoxOps.
    def __init__(self, settings: Settings, remote: Path):
        super().__init__(settings)
        self.remote = remote

    # Converts the example GitHub address into the path of the local test repository.
    # Rejects every other address so this example cannot push somewhere unexpected.
    def transport(self, url: str) -> str:
        if url != "https://github.com/example/project.git":
            raise ValueError("Offline demo transport identity changed")
        return str(self.remote)

    # Lets the example push to a local folder instead of a GitHub server.
    # Adds permission for local Git connections while keeping the other Git protections.
    def config_overrides(self, path: Path, environment: dict[str, str]) -> list[str]:
        return super().config_overrides(path, environment) + ["-c", "protocol.file.allow=always"]


# Pretends to be GitHub so the example does not need an account or internet access.
# Remembers whether its example repository exists and how many times it was created.
class FakeGitHub:
    # Sets up the pretend repository used by this example.
    # Remembers whether it exists and starts its creation counter at zero.
    def __init__(self, exists: bool = True):
        self.exists, self.creation_id, self.creations = exists, None, 0

    # Returns the pretend account name used throughout this example.
    # Does not contact GitHub or ask you to log in.
    async def probe(self, require_create: bool = False) -> str:
        return "example"

    # Returns details for the pretend repository if it has been created.
    # Checks the requested owner, name, and creation marker before reporting it as private.
    async def lookup(
        self, target: RepositoryTarget, creation_id: str | None = None
    ) -> RepositoryTarget | None:
        assert target.owner == "example" and target.name == "project"
        if creation_id and self.exists:
            assert creation_id == self.creation_id
        return (
            RepositoryTarget(
                owner="example",
                name="project",
                private=True,
                can_push=True,
                repository_id="offline-demo",
                clone_url="https://github.com/example/project.git",
            )
            if self.exists
            else None
        )

    # Pretends to create the private repository after the example approves it.
    # Marks it as existing and counts the creation without contacting GitHub.
    async def create(self, target: RepositoryTarget, creation_id: str | None = None) -> None:
        assert not self.exists and target.private
        self.exists, self.creation_id = True, creation_id
        self.creations += 1


# Creates a pretend AI that returns known file contents for this example.
# Lets the real generation code run without spending money on an AI service.
def demo_agent() -> Agent:
    # Reads which file the example requested and returns matching example text.
    # Produces a Dockerfile, ignore file, or CI file in the format the generator expects.
    async def respond(messages, info):
        prompt = next(part.content for part in messages[-1].parts if hasattr(part, "content"))
        packet = json.loads(prompt)
        task = packet["task"]
        unit = packet["context"]["requirements"]["units"][0]
        ignore = ".env\n.env.*\n.git/\n*.pem\n*.key\nnode_modules/\n.venv/\n"
        runtime = unit["runtime"]
        command = runtime.get("start_command") or "uvicorn main:app"
        docker = (
            "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\nUSER 1000\n"
            f"CMD {command} --host 0.0.0.0\n"
        )
        builds = task["expected_commands"] or ["python -m compileall main.py"]
        steps = "\n".join(f"      - run: {command}" for command in builds)
        ci = (
            "name: build\non: [push, pull_request]\npermissions:\n  contents: read\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
            "        with:\n          persist-credentials: false\n" + steps + "\n"
        )
        content = {"gitignore": ignore, "dockerignore": ignore, "dockerfile": docker, "ci": ci}[
            task["artifact_type"]
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


# Creates the real VoxOps app around a copied example project and local Git repository.
# Replaces only the AI and GitHub connections so the example cannot publish online.
def demo_app(directory: Path, fixture: str = "fastapi_only", bootstrap: bool = False):
    source = directory / "source"
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    shutil.copytree(fixtures / fixture, source)
    remote = directory / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "-C", str(remote), "init", "--bare", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    if not bootstrap:
        subprocess.run(
            ["git", "-C", str(source), "init", "--initial-branch=main"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "add", "--", *capture_source(source)[1]],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Demo",
                "-c",
                "user.email=demo@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                "Offline fixture baseline",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "remote",
                "add",
                "origin",
                "https://github.com/example/project.git",
            ],
            check=True,
            capture_output=True,
        )
    settings = Settings(allowed_roots=[directory], data_dir=directory / "records")
    app = create_app(settings)
    app.state.worktrees.root = directory / "worktrees"
    store = ArtifactStore(settings.data_dir)
    publisher = GitPublisher(
        settings,
        store,
        app.state.worktrees,
        connection=FakeGitHub(exists=not bootstrap),
        executor=LocalGit(settings, remote),
    )
    app.state.artifacts = ArtifactService(
        settings,
        app.state.store,
        app.state.worktrees,
        ArtifactGenerator(settings, store, demo_agent()),
        publisher,
    )
    app.state.artifacts.driver_factory = (
        lambda unit_id, journal, cancel, session: DemoDocker(settings, journal, cancel, session)
    )
    return app, source, remote


# Reads a successful API response or stops the example if the request failed.
# Includes the response in the error so a failed step is visible.
def response_json(response):
    if response.status_code >= 400:
        raise RuntimeError(f"Demo API failed with HTTP {response.status_code}: {response.json()}")
    return response.json()


# Runs the example through planning, file generation, review, and approval or rejection.
# Checks the actual local Git commit and confirms the original project files stayed unchanged.
def run_demo(
    directory: Path, reject: bool = False, bootstrap: bool = False, fixture: str = "fastapi_only"
) -> dict:
    app, source, remote = demo_app(directory, fixture=fixture, bootstrap=bootstrap)
    before = capture_source(source)[0]
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        analysis = response_json(
            client.post("/api/v1/repositories/analyze", json={"path": str(source)})
        )
        strategies = {
            unit["id"]: "static" if unit["framework"] == "react_vite" else "container"
            for unit in analysis["detected_units"]
        }
        plan = response_json(
            client.post(
                "/api/v1/artifact-runs/plan",
                json={
                    "path": str(source),
                    "strategies": strategies,
                },
            )
        )
        body = {"plan_id": plan["plan_id"], "owner": "example", "name": "project"}
        if bootstrap:
            body["selected_paths"] = list(capture_source(source)[1])
        else:
            body["selected_paths"] = []
        run = response_json(client.post("/api/v1/artifact-runs", json=body))
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
        if run["status"] == "awaiting_source_publication":
            review = response_json(client.get(prefix + "/review"))["review"]
            response_json(
                client.post(
                    prefix + "/approvals",
                    json={
                        "review_id": review["review_id"],
                        "digest": review["digest"],
                        "decision": "approve",
                    },
                )
            )
        run = response_json(client.post(prefix + "/execute"))
        if run["status"] != "awaiting_review":
            raise RuntimeError(f"Generation did not reach review: {run['errors']}")
        review = response_json(client.get(prefix + "/review"))["review"]
        assert review["diff"] and review["ci_may_run"]
        response_json(
            client.post(
                prefix + "/approvals",
                json={
                    "review_id": review["review_id"],
                    "digest": review["digest"],
                    "decision": "reject" if reject else "approve",
                },
            )
        )
        if reject:
            assert client.post(prefix + "/publish").status_code == 409
        else:
            response_json(client.post(prefix + "/publish"))
        result = response_json(client.get(prefix + "/result"))
        if not reject:
            sha = subprocess.run(
                ["git", "-C", str(remote), "rev-parse", f"refs/heads/{run['branch']}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert sha == result["result"]["remote_sha"] == result["result"]["commit_sha"]
        assert capture_source(source)[0] == before
        result["source_preserved"] = True
        result["offline"] = True
        result["reviewed_paths"] = [file["path"] for file in review["files"]]
        return result


# Reads the example options and creates a new folder for its temporary project.
# Runs the offline example, prints its result, and keeps the folder only when requested.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        required=True,
        help="Required acknowledgement that model and MCP adapters are fake",
    )
    parser.add_argument("--reject", action="store_true", help="Reject the artifact review")
    parser.add_argument(
        "--fixture",
        choices=["fastapi_only", "vite_only"],
        default="fastapi_only",
        help="Checked-in fixture copied to the disposable repository",
    )
    parser.add_argument(
        "--bootstrap", action="store_true", help="Approve a fake private baseline first"
    )
    parser.add_argument(
        "--keep", type=Path, help="Create this new directory and retain the evidence"
    )
    args = parser.parse_args()
    if args.keep:
        args.keep.mkdir(mode=0o700, parents=False, exist_ok=False)
        result = run_demo(args.keep.resolve(), args.reject, args.bootstrap, args.fixture)
        result["evidence_directory"] = str(args.keep.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="voxops-stage2-demo-") as folder:
            result = run_demo(Path(folder), args.reject, args.bootstrap, args.fixture)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
