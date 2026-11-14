import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import ValidationError

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.planner import build_plan
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import RepositoryTarget
from voxops.github.connection import GitHubConnection
from voxops.repository.git_executor import GitExecutor
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.mongodb import database_for
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


# Make a tiny FastAPI project in a temporary directory and detect its startup command.
# Ask the configured model to write its Dockerfile using the real generation and validation code.
# Return the checks and request counts, then delete the sample and saved output automatically.
async def check_model(settings: Settings) -> dict:
    with TemporaryDirectory(prefix="voxops-model-check-") as directory:
        base = Path(directory)
        root = base / "project"
        root.mkdir()
        (root / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
        (root / "requirements.txt").write_text("fastapi>=0.115,<1\nuvicorn>=0.34,<1\n")
        local = settings.model_copy(update={"allowed_roots": [root]})
        workflow = Stage1Workflow(local, ScanStore(database_for(local)))
        analysis = await workflow.analyze(str(root))
        plan = build_plan(
            root, analysis, {unit.id: "container" for unit in analysis.detected_units}
        )
        if plan.unresolved_questions:
            raise AppError("sample_ambiguous", "The sample could not be planned without questions.")
        task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
        store = ArtifactStore(database_for(local))
        result = await ArtifactGenerator(local, store).generate(
            uuid4().hex, root, task, plan, ModelBudget(local)
        )
        return {
            "check": "model",
            "model": local.model,
            "status": "passed" if result.status == "passed" else "failed",
            "task_status": result.status,
            "error_category": result.error_category,
            "attempts": len(result.attempts),
            "requests": sum(attempt.usage.requests for attempt in result.attempts),
            "input_tokens": sum(attempt.usage.input_tokens for attempt in result.attempts),
            "output_tokens": sum(attempt.usage.output_tokens for attempt in result.attempts),
            "validations": [item.model_dump(mode="json") for item in result.validations],
        }


# Connect to the configured GitHub tool server and read the signed-in account and tool count.
# If a repository was supplied, check its identity, push permission, and main branch over HTTPS Git.
# Use a temporary Git directory and return metadata without creating or pushing a GitHub repository.
async def check_github(settings: Settings, target: RepositoryTarget | None) -> dict:
    connection = GitHubConnection(settings)
    identity = await connection.probe()
    result = {
        "check": "github",
        "status": "passed",
        "identity": identity,
        "tools_available": len(connection.tools),
    }
    if target is not None:
        verified = await connection.lookup(target)
        if verified is None:
            raise AppError("repository_not_found", "The selected repository was not found.")
        with TemporaryDirectory(prefix="voxops-git-check-") as directory:
            root = Path(directory)
            git = GitExecutor(settings)
            await asyncio.to_thread(git.run, root, "init")
            sha = await asyncio.to_thread(git.remote_sha, root, verified.clone_url, "main")
        result.update(
            repository=f"{verified.owner}/{verified.name}",
            private=verified.private,
            can_push=verified.can_push,
            branch="main",
            remote_sha=sha,
        )
    return result


# Run each selected check independently so a model failure does not hide the GitHub result.
# Report known application errors by category and hide unexpected text that may contain secrets.
# Print the results as JSON and return a failing exit code if any selected check failed.
async def run_checks(settings: Settings, args: argparse.Namespace, target: RepositoryTarget | None):
    results = []
    for name, selected in (("model", args.model), ("github", args.github)):
        if not selected:
            continue
        try:
            result = (
                await check_model(settings)
                if name == "model"
                else await check_github(settings, target)
            )
        except AppError as error:
            result = {"check": name, "status": "failed", "error_category": error.category}
        except Exception as error:
            result = {
                "check": name,
                "status": "failed",
                "error_category": "check_failed",
                "error_type": type(error).__name__,
            }
        results.append(result)
    print(json.dumps({"checks": results}, indent=2))
    return int(any(result["status"] != "passed" for result in results))


# Read the requested checks and reject a repository argument unless GitHub checking is enabled.
# Load settings from the process environment; use uv's --env-file option to load a local .env file.
# Start the checks and return their exit code without printing keys or publishing files.
def main() -> int:
    parser = argparse.ArgumentParser(description="Check live model and GitHub connections safely.")
    parser.add_argument(
        "--model", action="store_true", help="Generate and validate a sample Dockerfile"
    )
    parser.add_argument(
        "--github", action="store_true", help="Read GitHub identity and available tools"
    )
    parser.add_argument(
        "--repository", metavar="OWNER/NAME", help="Also check repository access and Git"
    )
    args = parser.parse_args()
    if not (args.model or args.github):
        parser.error("choose --model, --github, or both")
    if args.repository and not args.github:
        parser.error("--repository requires --github")
    target = None
    if args.repository:
        owner, separator, name = args.repository.partition("/")
        try:
            if not separator:
                raise ValueError
            target = RepositoryTarget(owner=owner, name=name)
        except ValueError:
            parser.error("--repository must be a valid OWNER/NAME")
    try:
        settings = Settings()
    except ValidationError:
        print(json.dumps({"status": "failed", "error_category": "settings_invalid"}))
        return 1
    return asyncio.run(run_checks(settings, args, target))


if __name__ == "__main__":
    raise SystemExit(main())
