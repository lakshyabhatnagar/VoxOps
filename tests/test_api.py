from conftest import committed_repo, copy_fixture, settings_for
from fastapi.testclient import TestClient

from voxops.main import create_app


# Why: Verify the actual API contract from liveness through analysis, voice and worktree cleanup.
# How: Send loopback requests through the ASGI app using real disposable Git fixtures.
def test_api_end_to_end(tmp_path):
    app = create_app(settings_for(tmp_path))
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "VoxOps API tester" in page.text and 'data-action="analyze"' in page.text
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["ready"]
    root = copy_fixture(tmp_path, "monorepo_fastapi_vite")
    response = client.post(
        "/api/v1/repositories/analyze",
        json={"path": str(root), "transcript": "analyze this repository"},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["detected_units"]) == 2
    voice = client.post("/api/v1/voice/transcribe", json={"text": "analyze this repository"})
    assert voice.json()["provider"] == "transcript_stub"
    assert client.post("/api/v1/voice/transcribe", json={"mode": "gnani"}).status_code == 503
    source = committed_repo(tmp_path)
    response = client.post("/api/v1/worktrees", json={"path": str(source)})
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    assert client.get(f"/api/v1/worktrees/{identifier}").status_code == 200
    assert client.delete(f"/api/v1/worktrees/{identifier}").json()["status"] == "removed"
    bad = client.post("/api/v1/repositories/analyze", json={"path": 42, "secret": "sentinel"})
    assert bad.status_code == 422 and "sentinel" not in bad.text


# Why: Protect the trusted local executor from network exposure and browser-origin attacks.
# How: Reject remote socket peers, forged Host headers and cross-origin requests before routing.
def test_api_local_only(tmp_path):
    app = create_app(settings_for(tmp_path))
    remote = TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.4", 50000))
    assert remote.get("/health").status_code == 403
    assert remote.get("/").status_code == 403
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/health", headers={"origin": "https://evil.example"}).status_code == 403
