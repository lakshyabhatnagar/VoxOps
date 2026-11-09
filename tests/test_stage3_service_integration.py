import asyncio
from uuid import uuid4

from conftest import copy_fixture, settings_for
from demo_stage2 import demo_app
from fastapi.testclient import TestClient
from simulated_docker import DemoDocker
from test_artifact_generation import CI, offline_agent
from test_execution_adapters import AdapterDocker
from test_stage2_git import FakeGitHub, LocalGit, git

from voxops.artifacts.generator import ArtifactGenerator
from voxops.artifacts.planner import build_plan
from voxops.artifacts.service import ArtifactService
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, RepositoryTarget
from voxops.github.publisher import GitPublisher
from voxops.main import create_app
from voxops.repository.path_policy import fingerprint
from voxops.repository.snapshot import capture_source
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


def integration_fixture(tmp_path):
    source = copy_fixture(tmp_path, "vite_only")
    (source / ".env.example").unlink()
    (source / "config.js").write_text(
        'if (!process.env.VITE_REQUIRED) throw new Error("required");\n'
    )
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare", "--initial-branch=main")
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    git(source, "add", ".")
    git(source, "commit", "-m", "baseline")
    git(source, "push", str(bare), "main")
    git(source, "remote", "add", "origin", "https://github.com/example/project.git")
    settings = settings_for(tmp_path)
    scans, store = ScanStore(settings.data_dir), ArtifactStore(settings.data_dir)
    manager = WorktreeManager(settings, scans)
    manager.root = tmp_path / "worktrees"
    publisher = GitPublisher(settings, store, manager, FakeGitHub(True), LocalGit(settings, bare))
    analysis = asyncio.run(Stage1Workflow(settings, scans).analyze(str(source)))
    plan = build_plan(source, analysis, {analysis.detected_units[0].id: "static"})
    source_hash, _ = capture_source(source)
    run_id = uuid4().hex
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=fingerprint(source),
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint=source_hash,
        source_head_sha=publisher.executor.head(source),
        plan=plan,
        target=RepositoryTarget(owner="example", name="project"),
        branch=f"voxops/{run_id}",
    )
    store.bind_source(run_id, source)
    store.save_run(run)
    asyncio.run(publisher.preflight(run, []))
    generator = ArtifactGenerator(
        settings,
        store,
        offline_agent(
            lambda packet, content: (
                CI.replace("python -m compileall main.py", "pnpm run build")
                if packet["task"]["artifact_type"] == "ci"
                else content
            )
        ),
    )
    service = ArtifactService(settings, scans, manager, generator, publisher)

    def driver(unit_id, journal, cancel, session):
        instance = AdapterDocker(settings, cancel)
        instance.journal, instance.redact = journal, session.redact
        return instance

    service.driver_factory = driver
    return service, run, bare


def test_api_environment_verification_review_and_local_push(tmp_path):
    service, run, bare = integration_fixture(tmp_path)
    app = create_app(service.settings)
    app.state.artifacts = service
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        prefix = f"/api/v1/artifact-runs/{run.run_id}"

        def request(method, suffix, body=None):
            response = client.request(method, prefix + suffix, json=body)
            assert response.status_code == 200, response.text
            return response.json()

        result = request("POST", "/execute")
        assert result["execution"]["status"] == "waiting_for_env", result["errors"]
        form = request("GET", "/verification/env-form")
        field = next(
            item for item in form["schema"]["requirements"] if item["name"] == "VITE_REQUIRED"
        )
        invalid = client.post(
            prefix + "/verification/env-values",
            json={
                "schema_digest": form["schema_digest"],
                "values": {},
            },
        )
        assert invalid.status_code == 422
        request(
            "POST",
            "/verification/env-values",
            {
                "schema_digest": form["schema_digest"],
                "values": {field["field_id"]: "synthetic-secret-value"},
            },
        )
        result = request("POST", "/execute")
        assert result["execution"]["status"] == "passed", result["errors"]
        review = request("GET", "/review")["review"]
        request(
            "POST",
            "/approvals",
            {
                "review_id": review["review_id"],
                "digest": review["digest"],
                "decision": "approve",
            },
        )
        published = request("POST", "/publish")
        assert published["result"]["remote_sha"] == published["result"]["commit_sha"]
        assert published["result"]["deployment_executed"] is False
        assert "synthetic-secret-value" not in str(published)
        assert (
            git(bare, "rev-parse", f"refs/heads/{run.branch}").decode().strip()
            == published["result"]["remote_sha"]
        )


def test_external_service_incomplete_requires_acknowledgement(tmp_path):
    app, source, bare = demo_app(tmp_path, bootstrap=True)
    (source / "main.py").write_text(
        "import os\nfrom fastapi import FastAPI\n"
        "DATABASE_URL = os.environ['DATABASE_URL']\napp = FastAPI()\n"
    )
    service = app.state.artifacts

    class UnavailableService(DemoDocker):
        async def runtime(self, *args, **kwargs):
            raise AppError("UNKNOWN_RUNTIME_FAILURE", "DATABASE_URL connection refused", 409)

    service.driver_factory = lambda unit, journal, cancel, session: UnavailableService(
        service.settings, journal, cancel, session
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        analysis = client.post("/api/v1/repositories/analyze", json={"path": str(source)}).json()
        plan = client.post(
            "/api/v1/artifact-runs/plan",
            json={
                "path": str(source),
                "strategies": {analysis["detected_units"][0]["id"]: "container"},
            },
        ).json()
        run = client.post(
            "/api/v1/artifact-runs",
            json={
                "plan_id": plan["plan_id"],
                "owner": "example",
                "name": "project",
                "selected_paths": list(capture_source(source)[1]),
            },
        ).json()
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
        baseline = client.get(prefix + "/review").json()["review"]
        assert (
            client.post(
                prefix + "/approvals",
                json={
                    "review_id": baseline["review_id"],
                    "digest": baseline["digest"],
                    "decision": "approve",
                },
            ).status_code
            == 200
        )
        client.post(prefix + "/execute")
        form = client.get(prefix + "/verification/env-form").json()
        field = next(
            item for item in form["schema"]["requirements"] if item["name"] == "DATABASE_URL"
        )
        assert (
            client.post(
                prefix + "/verification/env-values",
                json={
                    "schema_digest": form["schema_digest"],
                    "values": {field["field_id"]: "postgresql://fixture.invalid/database"},
                },
            ).status_code
            == 200
        )
        result = client.post(prefix + "/execute").json()
        assert result["execution"]["status"] == "verification_incomplete", result["execution"][
            "units"
        ]
        review = client.get(prefix + "/review").json()["review"]
        approval = {
            "review_id": review["review_id"],
            "digest": review["digest"],
            "decision": "approve",
        }
        assert client.post(prefix + "/approvals", json=approval).status_code == 409
        assert client.post(prefix + "/publish").status_code == 409
        attestation = review["execution_attestation"]
        missing = [
            check["check_id"]
            for unit in attestation["units"].values()
            for check in unit["checks"].values()
            if check["status"] != "passed"
        ]
        approval["incomplete_acknowledgement"] = {
            "review_id": review["review_id"],
            "review_digest": review["digest"],
            "attestation_digest": attestation["digest"],
            "missing_check_ids": missing,
            "acknowledged": True,
        }
        assert client.post(prefix + "/approvals", json=approval).status_code == 200
        published = client.post(prefix + "/publish")
        assert published.status_code == 200, published.text
        result = published.json()["result"]
        assert result["verification_status"] == "verification_incomplete"
        assert git(bare, "rev-parse", result["branch"]).decode().strip() == result["remote_sha"]
