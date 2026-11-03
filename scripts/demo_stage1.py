"""Run against an already-started local VoxOps API; no project scripts are executed."""

import argparse
import json
from urllib.parse import urlsplit

import httpx


# Why: Demonstrate analysis and explicit temporary worktree setup without cloud access.
# How: Submit typed JSON to loopback endpoints and clean up only the newly created worktree.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Absolute path within the server's configured workspace roots")
    parser.add_argument("--transcript", help="Development text; never include credentials")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument(
        "--worktree", action="store_true", help="Create, inspect and remove a clean worktree"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    url = urlsplit(arguments.url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("The development harness requires an HTTP loopback API URL")
    with httpx.Client(base_url=arguments.url, timeout=90, trust_env=False) as client:
        body = {"path": arguments.path, "use_llm": arguments.use_llm}
        if arguments.transcript:
            voice = client.post("/api/v1/voice/transcribe", json={"text": arguments.transcript})
            check_response(voice)
            body["transcript"] = voice.json()["text"]
        analysis = client.post("/api/v1/repositories/analyze", json=body)
        check_response(analysis)
        print(json.dumps(analysis.json(), indent=2))
        if arguments.worktree:
            created = client.post("/api/v1/worktrees", json={"path": arguments.path})
            check_response(created)
            identifier = created.json()["id"]
            try:
                inspected = client.get(f"/api/v1/worktrees/{identifier}")
                check_response(inspected)
                print(json.dumps({"worktree": inspected.json()}, indent=2))
            finally:
                removed = client.delete(f"/api/v1/worktrees/{identifier}")
                check_response(removed)
                print(json.dumps({"cleanup": removed.json()}, indent=2))


# Why: Keep CLI failures actionable without dumping request paths or transport internals.
# How: Print the API's already-sanitized error envelope and terminate on failure status.
def check_response(response: httpx.Response) -> None:
    if response.is_error:
        print(json.dumps(response.json(), indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
