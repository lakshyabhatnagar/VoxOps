from pathlib import Path

import pytest
from demo_stage2 import demo_app, response_json, run_demo
from fastapi.testclient import TestClient


@pytest.mark.parametrize(("bootstrap", "reject"), [(False, False), (True, False), (False, True)])
# Run the local HTTP workflow with the selected approval and setup options.
# Check its result and confirm that the source remains unchanged.
def test_offline_http_workflow(tmp_path, bootstrap, reject):
    result = run_demo(tmp_path, reject=reject, bootstrap=bootstrap)
    assert result["source_preserved"] and result["offline"]
    assert result["deployment_executed"] is False
    assert result["status"] == ("rejected" if reject else "ready_for_deployment")
    if reject:
        assert result["result"] is None
    else:
        assert result["result"]["commit_sha"] == result["result"]["remote_sha"]
        assert result["result"]["branch"].startswith("voxops/")


# Reject the first source review and try requests that should not reach the local API.
# Check that rejection prevents publication and unsafe requests are blocked.
def test_rejected_baseline_and_local_api_protection(tmp_path):
    app, source, remote = demo_app(tmp_path, bootstrap=True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as client:
        analysis = response_json(
            client.post("/api/v1/repositories/analyze", json={"path": str(source)})
        )
        plan = response_json(
            client.post(
                "/api/v1/artifact-runs/plan",
                json={
                    "path": str(source),
                    "strategies": {analysis["detected_units"][0]["id"]: "container"},
                },
            )
        )
        run = response_json(
            client.post(
                "/api/v1/artifact-runs",
                json={
                    "plan_id": plan["plan_id"],
                    "owner": "example",
                    "name": "project",
                    "selected_paths": ["main.py", "requirements.txt"],
                },
            )
        )
        assert run["status"] == "awaiting_source_publication" and not (source / ".git").exists()
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
        review = response_json(client.get(prefix + "/review"))["review"]
        response_json(
            client.post(
                prefix + "/approvals",
                json={
                    "review_id": review["review_id"],
                    "digest": review["digest"],
                    "decision": "reject",
                },
            )
        )
        assert client.post(prefix + "/publish").status_code == 409
        assert client.post(prefix + "/source-selection", json={"paths": []}).status_code == 409
        assert response_json(client.post(prefix + "/execute"))["status"] == "rejected"
        assert not (source / ".git").exists()
        assert app.state.artifacts.publisher.connection.creations == 0
        assert client.get(prefix, headers={"Origin": "https://hostile.invalid"}).status_code == 403


# Approve a selection of source files that omits a required build file.
# Check that generation stops before sending a model request.
def test_selected_baseline_missing_build_input_blocks_generation(tmp_path):
    app, source, _ = demo_app(tmp_path, bootstrap=True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50003)) as client:
        analysis = response_json(
            client.post("/api/v1/repositories/analyze", json={"path": str(source)})
        )
        plan = response_json(
            client.post(
                "/api/v1/artifact-runs/plan",
                json={
                    "path": str(source),
                    "strategies": {analysis["detected_units"][0]["id"]: "container"},
                },
            )
        )
        run = response_json(
            client.post(
                "/api/v1/artifact-runs",
                json={
                    "plan_id": plan["plan_id"],
                    "owner": "example",
                    "name": "project",
                    "selected_paths": ["main.py"],
                },
            )
        )
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
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
        assert run["status"] == "blocked" and run["errors"] == ["required_source_missing"]
        assert not run["tasks"] and run["model_usage"]["requests"] == 0
        assert (source / "requirements.txt").exists()


# Select one locally changed source file through the HTTP API.
# Check that the selected content is used and omitted changes stay in the source.
def test_dirty_snapshot_http_selection(tmp_path):
    app, source, _ = demo_app(tmp_path)
    original = (source / "main.py").read_text() + "\n# selected local change\n"
    (source / "main.py").write_text(original)
    (source / "notes.txt").write_text("omitted local notes\n")
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50002)) as client:
        analysis = response_json(
            client.post("/api/v1/repositories/analyze", json={"path": str(source)})
        )
        plan = response_json(
            client.post(
                "/api/v1/artifact-runs/plan",
                json={
                    "path": str(source),
                    "strategies": {analysis["detected_units"][0]["id"]: "container"},
                },
            )
        )
        run = response_json(
            client.post(
                "/api/v1/artifact-runs",
                json={
                    "plan_id": plan["plan_id"],
                    "owner": "example",
                    "name": "project",
                },
            )
        )
        prefix = f"/api/v1/artifact-runs/{run['run_id']}"
        assert run["status"] == "awaiting_source_publication"
        response_json(client.post(prefix + "/source-selection", json={"paths": ["main.py"]}))
        review = response_json(client.get(prefix + "/review"))["review"]
        assert review["purpose"] == "source_snapshot" and "notes.txt" in review["excluded_paths"]
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
        assert run["status"] == "awaiting_review", run["errors"]
        location = Path(app.state.worktrees.inspect(run["worktree_id"]).location)
        assert (location / "main.py").read_text() == original
        assert not (location / "notes.txt").exists()
        assert (source / "notes.txt").read_text() == "omitted local notes\n"
        assert (source / "main.py").read_text() == original


# Acquire a project lock and then try to acquire it again.
# Check that the second writer receives a busy error.
def test_project_lock_rejects_second_writer(tmp_path):
    from voxops.core.errors import AppError
    from voxops.storage.artifact_store import ArtifactStore

    first, second = ArtifactStore(tmp_path), ArtifactStore(tmp_path)
    with first.project_lock("a" * 24), pytest.raises(AppError, match="Another operation"):
        with second.project_lock("a" * 24):
            pytest.fail("Second writer acquired the project")
