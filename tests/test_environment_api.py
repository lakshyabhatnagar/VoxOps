from fastapi.testclient import TestClient
from test_stage3_gate import identity, prepared, setup_execution

from voxops.artifacts.service import ArtifactService
from voxops.main import create_app
from voxops.repository.snapshot import capture_source
from voxops.storage.scan_store import ScanStore


def test_form_and_submission_use_real_run_and_local_middleware(tmp_path):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location, schema_only=True)
    (location / "settings.py").write_text(
        "import os\nTOKEN = os.environ['TOKEN']\nPORT = os.getenv('PORT', '8000')\n"
    )
    ctx.run.execution.attestation = None
    ctx.run.execution.identity = identity(ctx, location)
    ctx.store.save_run(ctx.run)
    settings = ctx.publisher.settings
    service = ArtifactService(
        settings, ScanStore(settings.data_dir), ctx.manager, publisher=ctx.publisher
    )
    service.prepare_environment(ctx.run.run_id)
    app = create_app(settings)
    app.state.artifacts = service
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    url = f"/api/v1/artifact-runs/{ctx.run.run_id}/verification"
    response = client.get(f"{url}/env-form")
    assert response.status_code == 200, response.text
    form = response.json()
    token = next(field for field in form["schema"]["requirements"] if field["name"] == "TOKEN")
    assert token["necessity"] == "required"
    payload = {
        "schema_digest": form["schema_digest"],
        "values": {token["field_id"]: "private-secret"},
    }
    response = client.post(f"{url}/env-values", json=payload)
    assert response.status_code == 200, response.text
    assert "private-secret" not in response.text
    saved = ctx.store.load_run(ctx.run.run_id)
    assert "private-secret" not in saved.model_dump_json()
    assert saved.execution.attestation is None
    assert saved.execution.env_schema.scan_id == form["schema"]["scan_id"]
    assert service.environment.session(saved).values_for("app", "runtime", "secret") == {
        "TOKEN": "private-secret"
    }
    stale = client.post(f"{url}/env-values", json={**payload, "schema_digest": "0" * 64})
    assert stale.status_code == 409
    denied = TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.4", 1))
    assert denied.get(f"{url}/env-form").status_code == 403
    (location / "settings.py").write_text("changed = True\n")
    assert capture_source(location)[1] != {}
    assert client.get(f"{url}/env-form").status_code == 409
