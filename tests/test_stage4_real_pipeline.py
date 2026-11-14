import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from bson import BSON
from conftest import git_test
from fastapi.testclient import TestClient
from pymongo import MongoClient
from test_artifact_generation import IGNORES, offline_agent
from test_stage2_git import FakeGitHub, LocalGit

from voxops.core.settings import Settings
from voxops.main import create_app
from voxops.repository.acquisition import RepositoryAcquirer
from voxops.repository.snapshot import capture_source
from voxops.storage import mongodb


# Poll the actual workflow HTTP endpoint until it pauses or finishes.
# Fail with its safe diagnostics rather than silently treating a blocked build as success.
def wait_status(client, workflow_id, wanted, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/workflows/{workflow_id}")
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] in wanted:
            return state
        assert state["status"] not in {"failed", "expired", "cancelled", "rejected"}, state
        time.sleep(0.2)
    raise AssertionError("Workflow did not reach the requested state before the smoke deadline")


# Return explicit generation fixtures while using real Docker to detect and repair a missing COPY.
# Repository code and model failure feedback pass through the actual Stage 2 generator/validators.
def real_pipeline_agent(calls):
    # Choose content from the requested unit and its real detected build/start commands.
    # The first API image intentionally refers to a missing ARG path that Docker must reject.
    def content(packet, original):
        task = packet["task"]
        calls.append((task["target_path"], bool(packet.get("execution_feedback"))))
        if task["artifact_type"] in {"gitignore", "dockerignore"}:
            return IGNORES
        if task["artifact_type"] == "ci":
            return (
                "name: build\non: [push, pull_request]\npermissions:\n  contents: read\n"
                "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
                "        with:\n          persist-credentials: false\n"
                "      - run: npm install --no-audit --no-fund\n        working-directory: web\n"
                "      - run: npm run build\n        working-directory: web\n"
            )
        if task["target_path"] == "api/Dockerfile":
            application = (
                "api/main.py" if packet.get("execution_feedback") else "api/missing_application.py"
            )
            return (
                "FROM python:3.12-slim\nWORKDIR /app\n"
                f"ARG APPLICATION_FILE={application}\nCOPY api/requirements.txt .\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY ${APPLICATION_FILE} main.py\nUSER 1000\nEXPOSE 8000\n"
                'CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]\n'
            )
        return (
            "FROM node:22-bookworm-slim\nWORKDIR /app\nCOPY web/package.json .\n"
            "RUN npm install --no-audit --no-fund\nCOPY web/ .\nRUN npm run build\n"
            "USER node\nEXPOSE 4173\n"
            'CMD ["npm","run","start","--","--host","0.0.0.0","--port","4173"]\n'
        )

    return offline_agent(content)


@pytest.mark.skipif(
    not os.environ.get("VOXOPS_TEST_MONGO_URI") or os.environ.get("VOXOPS_TEST_REAL_DOCKER") != "1",
    reason="real MongoDB and Docker smoke opt-in",
)
# Exercise acquisition, two real builds/servers, native pauses, repair and approved push.
# GitHub transport/metadata and model responses are explicit fixtures; MongoDB/Docker/Git are real.
def test_real_stage4_workflow_mongo_docker_and_git(tmp_path, monkeypatch):
    root = tmp_path / "upstream"
    (root / "api").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "api/requirements.txt").write_text("fastapi==0.115.12\nuvicorn==0.34.3\n")
    (root / "api/main.py").write_text(
        "import os\nfrom fastapi import FastAPI\n"
        "SERVICE_TOKEN = os.environ['SERVICE_TOKEN']\napp = FastAPI()\n"
        "# Return a simple response so the real readiness probe can reach this app.\n"
        "# No configuration values enter its response or logs.\n"
        "@app.get('/')\ndef ready():\n    return {'ready': True}\n"
    )
    (root / "web/package.json").write_text(
        json.dumps(
            {
                "name": "voxops-stage4-real-web",
                "private": True,
                "type": "module",
                "scripts": {"build": "vite build", "start": "vite preview"},
                "dependencies": {"vite": "5.4.21", "react": "19.1.0"},
            }
        )
    )
    (root / "web/index.html").write_text(
        '<html><body><div id="root"></div>'
        '<script type="module" src="/main.js"></script></body></html>'
    )
    (root / "web/main.js").write_text(
        'import React from "react"; document.getElementById("root").textContent = React.version;'
    )
    git_test(root, "init", "--initial-branch=main")
    git_test(root, "add", ".")
    git_test(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "real Stage 4 fixture",
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git_test(remote, "init", "--bare", "--initial-branch=main")
    git_test(root, "push", str(remote), "main")
    before, source_sha = capture_source(root)[0], git_test(root, "rev-parse", "HEAD")
    name = "voxops_real_" + uuid4().hex
    uri = os.environ["VOXOPS_TEST_MONGO_URI"]
    settings = Settings(
        allowed_roots=[tmp_path],
        mongo_uri=uri,
        mongo_database=name,
        execution_build_memory_mb=2048,
        build_timeout_seconds=600,
    )
    monkeypatch.setattr(mongodb, "MongoClient", MongoClient)

    # Map only Git transport to the disposable bare remote while keeping acquisition code real.
    # Production still accepts only GitHub HTTPS URLs and retains the same credential protections.
    def acquire_fixture(config, database):
        return RepositoryAcquirer(config, database, LocalGit(config, remote))

    monkeypatch.setattr("voxops.main.RepositoryAcquirer", acquire_fixture)
    app = create_app(settings)
    service = app.state.artifacts
    service.publisher.connection = FakeGitHub(True)
    service.publisher.executor = LocalGit(settings, remote)
    calls = []
    service.generator.agent = real_pipeline_agent(calls)
    secret = "stage4-private-runtime-value"
    workflow_id = None
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
            created = client.post(
                "/api/v1/workflows",
                headers={"Idempotency-Key": uuid4().hex},
                json={"repository_url": "https://github.com/example/project", "ref": "main"},
            )
            assert created.status_code == 202, created.text
            workflow_id = created.json()["workflow_id"]
            waiting = wait_status(client, workflow_id, {"waiting_for_env"})
            run_id = waiting["artifact_run_id"]
            env_url = f"/api/v1/artifact-runs/{run_id}/verification"
            form = client.get(env_url + "/env-form").json()
            field = next(
                item for item in form["schema"]["requirements"] if item["name"] == "SERVICE_TOKEN"
            )
            supplied = client.post(
                env_url + "/env-values",
                json={
                    "schema_digest": form["schema_digest"],
                    "values": {field["field_id"]: secret},
                },
            )
            assert supplied.status_code == 200, supplied.text
            resumed = client.post(
                f"/api/v1/workflows/{workflow_id}/resume",
                json={"schema_digest": form["schema_digest"]},
            )
            assert resumed.status_code == 202, resumed.text
            checked = wait_status(client, workflow_id, {"waiting_for_approval"})
            assert len(checked["units"]) == 2
            assert all(unit["status"] == "passed" for unit in checked["units"].values())
            review = checked["pending"][0]
            approved = client.post(
                f"/api/v1/workflows/{workflow_id}/resume",
                json={
                    "review_id": review["review_id"],
                    "digest": review["digest"],
                    "decision": "approve",
                },
            )
            assert approved.status_code == 202, approved.text
            final = wait_status(client, workflow_id, {"ready_for_deployment"})
            deadline = time.monotonic() + 20
            while not final["cleanup_complete"] and time.monotonic() < deadline:
                time.sleep(0.2)
                final = client.get(f"/api/v1/workflows/{workflow_id}").json()
            assert final["cleanup_complete"] and final["cleanup_error"] is None, final
            run = service.store.load_run(run_id)
            assert len(run.execution.units) == 2 and sum(run.repair_cycles.values()) == 1
            assert any(repair for _, repair in calls)
            assert all(item.state == "removed" for item in run.execution.resources)
            assert (
                git_test(remote, "rev-parse", f"refs/heads/{run.branch}")
                == final["result"]["remote_sha"]
            )
            assert (
                capture_source(root)[0] == before
                and git_test(root, "rev-parse", "HEAD") == source_sha
            )
            for collection in service.store.database.list_collection_names():
                assert all(
                    secret.encode() not in BSON.encode(doc)
                    for doc in service.store.database[collection].find()
                )
            print(
                json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "units": 2,
                        "repair_cycles": run.repair_cycles,
                        "remote_sha": run.result.remote_sha,
                        "cleanup_complete": True,
                        "real": ["MongoDB", "Docker builds", "HTTP probes", "Git"],
                        "fixtures": ["GitHub transport/metadata", "model generation/repair"],
                    }
                )
            )
        environment = {
            **os.environ,
            "VOXOPS_TEST_DATABASE": name,
            "VOXOPS_TEST_WORKFLOW": workflow_id,
        }
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; from pymongo import MongoClient; "
                    "from langgraph.checkpoint.mongodb import MongoDBSaver; "
                    "c=MongoClient(os.environ['VOXOPS_TEST_MONGO_URI']); "
                    "db=c[os.environ['VOXOPS_TEST_DATABASE']]; "
                    "wid=os.environ['VOXOPS_TEST_WORKFLOW']; "
                    "assert db.workflows.find_one({'_id':wid})['status']=='ready_for_deployment'; "
                    "config={'configurable':{'thread_id':wid}}; "
                    "checkpoint=MongoDBSaver(c,db_name=db.name).get_tuple(config); "
                    "values=checkpoint.checkpoint['channel_values']; "
                    "assert len(values['unit_outcomes'])==2; c.close()"
                ),
            ],
            env=environment,
            check=True,
            timeout=20,
        )
    finally:
        if workflow_id and workflow_id in app.state.workflows.tasks:
            raise AssertionError("Application shutdown left a live workflow task")
        with MongoClient(uri) as cleanup_client:
            cleanup_client.drop_database(name)
