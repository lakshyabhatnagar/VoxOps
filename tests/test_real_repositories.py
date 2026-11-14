"""Pinned upstream cases, offline model replay, and opt-in real Docker execution."""

import asyncio
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import settings_for
from domain_steps import artifact_step
from fastapi.testclient import TestClient
from pipeline_fixtures import FakeGitHub, LocalGit
from simulated_docker import DemoDocker
from test_artifact_generation import offline_agent
from test_stage2_git import git

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.planner import build_plan
from voxops.artifacts.service import ArtifactService, apply_proposals
from voxops.artifacts.validators import validate_artifact, validate_cross_artifacts
from voxops.domain.artifact_models import ArtifactPlan, TaskResult
from voxops.domain.models import AnalysisResult
from voxops.github.publisher import GitPublisher
from voxops.main import create_app
from voxops.repository.path_policy import fingerprint
from voxops.repository.snapshot import capture_source
from voxops.workflows.stage1_graph import Stage1Workflow
from voxops.workflows.stage2_graph import Stage2Graph

FIXTURES = Path(__file__).parent / "fixtures/real_repositories"
CASES = [
    "fastapi_microsoft",
    "fastapi_package",
    "vite_typescript",
    "next_starter",
    "next_minimal",
    "next_static",
    "next_database",
]


# Send requests through the same local HTTP routes used by the testing frontend.
# Require the expected status and return JSON so each case can check the next step.
# Include response text on failure to make a broken gate understandable from pytest output.
def api(client, method, path, body=None, status=200):
    response = (
        artifact_step(
            client, "advance" if method == "ADVANCE" else "publish", path.rsplit("/", 1)[-1]
        )
        if method in {"ADVANCE", "PUBLISH"}
        else client.request(method, path, json=body)
    )
    assert response.status_code == status, response.text
    return response.json()


# Approve the current review through the normal API, including incomplete-check acknowledgement.
# The approval applies only to this disposable test repository and its displayed digest.
# Both source preparation and final publication use this helper without bypassing their gates.
def approve(client, prefix):
    review = api(client, "GET", prefix + "/review")["review"]
    body = {"review_id": review["review_id"], "digest": review["digest"], "decision": "approve"}
    report = review.get("execution_attestation")
    if report and report["status"] == "verification_incomplete":
        body["incomplete_acknowledgement"] = {
            "review_id": review["review_id"],
            "review_digest": review["digest"],
            "attestation_digest": review["attestation_digest"],
            "acknowledged": True,
            "missing_check_ids": [
                check["check_id"]
                for unit in report["units"].values()
                for check in unit["checks"].values()
                if check["status"] != "passed"
            ],
        }
    return api(client, "POST", prefix + "/approvals", body)


# Submit one form of synthetic values if the original source genuinely requires configuration.
# Use field types rather than real credentials, and leave unknown requiredness unresolved.
# This tests form handling without claiming that external databases or auth providers exist.
def supply_environment(client, prefix, run):
    if not run.get("execution") or run["execution"]["status"] != "waiting_for_env":
        return run
    form = api(client, "GET", prefix + "/verification/env-form")
    assert not form["schema"]["ambiguities"], form["schema"]["ambiguities"]
    values = {}
    for field in form["schema"]["requirements"]:
        if field["necessity"] != "required" or field["resolution"] != "missing":
            continue
        kind, name = field["value_type"], field["name"]
        value = {
            "port": "8000",
            "integer": "1",
            "boolean": "false",
            "url": "https://example.test",
        }.get(kind)
        if value is None:
            value = "targeted-fixture-only-value"
            if "EMAIL" in name:
                value = "fixture@example.test"
        values[field["field_id"]] = value
    api(
        client,
        "POST",
        prefix + "/verification/env-values",
        {"schema_digest": form["schema_digest"], "values": values},
    )
    return api(client, "ADVANCE", prefix)


# Exercise one pinned source revision with any earlier stage restored from local checkpoints.
# The default follows the API journey through verification, approval, and a local bare Git push.
# Only the external model, Docker commands, and GitHub metadata are simulated by default.
# The original source must remain unchanged, and blocked cases must never reach publication.
@pytest.mark.parametrize("case", CASES)
def test_repository_pipeline(tmp_path, request: pytest.FixtureRequest, case):
    stage = request.config.getoption("--repo-stage")
    real_docker = request.config.getoption("--repo-real-docker")
    fixture = FIXTURES / case
    metadata = json.loads((fixture / "upstream.json").read_text())
    checkpoint = json.loads((fixture / "stage2.json").read_text())
    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(fixture / "source.zip") as archive:
        archive.extractall(source)
        for info in archive.infolist():
            if info.external_attr >> 16 & 0o111:
                (source / info.filename).chmod(0o755)
    before = capture_source(source)[0]
    settings = settings_for(tmp_path, model="disabled")
    app = create_app(settings)
    app.state.worktrees.root = tmp_path / "worktrees"
    if stage in {"all", "1"}:
        analysis = asyncio.run(Stage1Workflow(settings, app.state.store).analyze(str(source)))
        assert [u.framework for u in analysis.detected_units] == metadata["frameworks"]
        assert [u.role for u in analysis.detected_units] == metadata["roles"]
    else:
        analysis = AnalysisResult.model_validate_json((fixture / "stage1.json").read_text())
    if stage == "1":
        return
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    git(source, "add", "-f", "--", *capture_source(source)[1])
    git(source, "commit", "-m", "Pinned upstream fixture")
    git(source, "push", str(remote), "main")
    git(source, "remote", "add", "origin", "https://github.com/example/project.git")
    head = git(source, "rev-parse", "HEAD")
    store = app.state.artifacts.store

    # Replay fixed file responses while keeping the real generator and its validators active.
    # The database case first returns the invented action hash seen in the original live run.
    # Returning the corrected CI only after feedback checks the actual retry path offline.
    def model_reply(packet, _):
        path = packet["task"]["target_path"]
        if (
            path == ".github/workflows/ci.yml"
            and checkpoint.get("rejected_ci")
            and not packet.get("previous_candidate")
        ):
            return checkpoint["rejected_ci"]
        return checkpoint["files"][path]

    generator = ArtifactGenerator(
        settings, store, None if stage == "3" else offline_agent(model_reply)
    )
    publisher = GitPublisher(
        settings, store, app.state.worktrees, FakeGitHub(True), LocalGit(settings, remote)
    )
    service = ArtifactService(settings, app.state.store, app.state.worktrees, generator, publisher)
    if not real_docker:
        service.driver_factory = lambda unit, journal, cancel, session: DemoDocker(
            settings, journal, cancel, session
        )
    app.state.artifacts = service
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        if stage == "all":
            transcript = api(
                client,
                "POST",
                "/api/v1/voice/transcribe",
                {
                    "mode": "transcript",
                    "text": "Prepare this app and show the changes before publishing.",
                },
            )
            assert transcript["provider"] == "transcript_stub"
            plan_json = api(
                client,
                "POST",
                "/api/v1/artifact-runs/plan",
                {
                    "path": str(source),
                    "strategies": {u.id: metadata["strategy"] for u in analysis.detected_units},
                },
            )
            plan = ArtifactPlan.model_validate(plan_json)
        else:
            analysis.scan.repo_fingerprint = fingerprint(source)
            plan = (
                ArtifactPlan.model_validate(checkpoint["plan"])
                if stage == "3"
                else build_plan(
                    source, analysis, {u.id: metadata["strategy"] for u in analysis.detected_units}
                )
            )
            plan.plan_id, plan.repo_fingerprint = uuid4().hex, fingerprint(source)
            store.save_plan(
                plan.plan_id,
                {
                    "schema_version": "1",
                    "plan": plan.model_dump(mode="json"),
                    "source": str(source),
                    "source_fingerprint": capture_source(source)[0],
                    "head": head.decode().strip(),
                },
            )
        assert not plan.unresolved_questions
        run = api(
            client,
            "POST",
            "/api/v1/artifact-runs",
            {"plan_id": plan.plan_id, "owner": "example", "name": "project", "selected_paths": []},
            status=201,
        )
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
        if run["status"] == "awaiting_source_publication":
            run = approve(client, prefix)
        api(client, "PUBLISH", prefix, status=409)
        if stage in {"2", "3"}:
            saved = store.load_run(run["run_id"])
            asyncio.run(publisher.prepare(saved))
            candidate = Path(app.state.worktrees.inspect(saved.worktree_id).location)
            if stage == "2":
                saved.tasks = asyncio.run(
                    Stage2Graph(settings, generator, ModelBudget(settings)).execute(
                        saved.run_id, candidate, plan
                    )
                )
            else:
                for key, task in plan.tasks.items():
                    if task.operation == "skip":
                        result = TaskResult(task_id=key, status="skipped")
                    else:
                        text = checkpoint["files"][task.target_path]
                        findings = validate_artifact(task, text, plan, root=candidate)
                        assert generator.valid(findings), findings
                        result = TaskResult(
                            task_id=key,
                            status="passed",
                            validations=findings,
                            proposal=generator.proposal(task, text, "llm", findings),
                        )
                    saved.tasks[key] = generator.persist(saved.run_id, result)
            assert all(
                t.status in {"passed", "validated_existing", "skipped"}
                for t in saved.tasks.values()
            )
            contents = {
                t.proposal.target_path: store.read_blob(t.proposal.staged_ref).decode()
                for t in saved.tasks.values()
                if t.proposal
            }
            assert generator.valid(validate_cross_artifacts(plan, contents, root=candidate))
            apply_proposals(candidate, saved, store)
            store.save_run(saved)
            if stage == "2":
                if case == "next_database":
                    assert len(saved.tasks["ci"].attempts) == 2
                assert capture_source(source)[0] == before
                return
        run = api(client, "ADVANCE", prefix)
        expected = metadata["expected_stage3"]
        if expected in {"blocked_unpinned_manager", "blocked_env_expression"}:
            error = (
                "INVALID_ARTIFACT"
                if expected == "blocked_unpinned_manager"
                else "invalid_env_values"
            )
            assert run["status"] == "blocked" and run["errors"] == [error], run["errors"]
            assert run["execution"] is None
            if case == "next_database" and stage != "3":
                assert len(run["tasks"]["ci"]["attempts"]) == 2
            api(client, "PUBLISH", prefix, status=409)
        elif expected == "waiting_for_env":
            assert run["execution"]["status"] == "waiting_for_env"
            form = api(client, "GET", prefix + "/verification/env-form")
            assert any(
                a["path"] == "fastapi_example/config.py" for a in form["schema"]["ambiguities"]
            )
            api(client, "PUBLISH", prefix, status=409)
        else:
            run = supply_environment(client, prefix, run)
            assert run["execution"] and run["execution"]["status"] == expected, run["errors"]
            api(client, "PUBLISH", prefix, status=409)
            approve(client, prefix)
            published = api(client, "PUBLISH", prefix)
            receipt = published["result"]
            assert (
                git(remote, "rev-parse", f"refs/heads/{receipt['branch']}").decode().strip()
                == receipt["commit_sha"]
                == receipt["remote_sha"]
            )
            assert receipt["deployment_executed"] is False
            assert not published["errors"]
            assert all(r["state"] == "removed" for r in published["execution"]["resources"])
            if stage == "3":
                assert published["model_usage"]["requests"] == 0
        assert capture_source(source)[0] == before
        assert git(source, "rev-parse", "HEAD") == head
