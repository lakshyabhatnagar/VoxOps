# Set up and test VoxOps integrations

This guide starts with local checks, then tests NVIDIA and GitHub separately. The final section uses the Stage 4 workflow with a disposable GitHub source. Approval can push a branch and trigger GitHub Actions. The checked-in Stage 1 fixture is for scanning; its `main.py` deliberately raises an error if executed.

## Prepare the workspace

Install Python 3.11 or later, `uv`, Git, Node.js, GitHub CLI (`gh`), `curl`, and `jq`. MongoDB is required. Docker is required for Stage 4 verification. Actionlint is optional. Run all commands from the VoxOps checkout unless a step says otherwise.

```sh
cd /home/lakshya/VoxOps
uv sync --locked
mkdir -p /tmp/voxops-integration-tests
docker run -d --name voxops-mongo -p 127.0.0.1:27017:27017 -v voxops-mongo:/data/db mongo:8
export VOXOPS_MONGO_URI=mongodb://127.0.0.1:27017
export VOXOPS_MONGO_DATABASE=voxops
export VOXOPS_ALLOWED_ROOTS='["/tmp/voxops-integration-tests","/home/lakshya/VoxOps/tests/fixtures"]'
```

Operational data lives in MongoDB. Add any other existing project directory to the JSON array before scanning it. For AEREO, include `/home/lakshya/AEREO` in that array. Do not add a nonexistent root; readiness requires all roots to exist. In `.env`, write the setting once, with a JSON array as its value:

```dotenv
VOXOPS_ALLOWED_ROOTS='["/home/lakshya/VoxOps","/home/lakshya/AEREO","/tmp/voxops-integration-tests"]'
```

The `export` above overrides `.env` for this terminal. To also scan AEREO while following this guide, use:

```sh
export VOXOPS_ALLOWED_ROOTS='["/tmp/voxops-integration-tests","/home/lakshya/VoxOps/tests/fixtures","/home/lakshya/AEREO"]'
```

Run the local checks:

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/check_comments.py
node tests/test_api_tester.mjs
```

Each command should finish successfully. Optional actionlint checks require actionlint and `VOXOPS_ARTIFACT_OPTIONAL_TOOLS=true`. These steps do not build a Docker image.

Run the focused offline workflow checks:

```sh
.venv/bin/pytest tests/test_workflow_api.py tests/test_stage4_graph.py -q
```

These tests use temporary source copies, typed model responses, simulated Docker,
GitHub metadata fixtures, native graph interrupts, and a real local bare Git remote.
They do not contact a model or GitHub. The real Docker test is separate and opt-in;
see [Stage 4 verification](stage4-verification.md).

## Configure and test NVIDIA

The local `.env` selects `nvidia:mistralai/mistral-large-4-1t-a52b`, a 60-second model timeout, and one artifact worker. NVIDIA requests use `https://integrate.api.nvidia.com/v1` and low reasoning effort for this model. [NVIDIA lists a free endpoint](https://build.nvidia.com/mistralai/mistral-large-4-1t-a52b), with [coding and structured replies supported](https://docs.api.nvidia.com/nim/reference/mistralai-mistral-large-4-1t-a52b). Account limits still apply.

Open the model page, sign in, and generate an API key. Add it to the existing blank `NVIDIA_API_KEY=` line in your ignored `.env` file. Keep these settings:

```dotenv
VOXOPS_MODEL=nvidia:mistralai/mistral-large-4-1t-a52b
VOXOPS_MODEL_TIMEOUT=60
VOXOPS_ARTIFACT_CONCURRENCY=1
NVIDIA_API_KEY=your-key-here
```

You can alternatively enter the key just for your current terminal:

```sh
read -r -s -p 'NVIDIA API key: ' NVIDIA_API_KEY; printf '\n'
export NVIDIA_API_KEY
uv run --env-file .env python scripts/check_integrations.py --model
```

If you saved the key in `.env`, run only the last command. The check creates a disposable FastAPI project and asks the real model for a Dockerfile. It may make up to three requests if the file fails checks. It checks the response and file contents without publishing to GitHub or building an image. Expect `status: "passed"`, at least one request, and passing validations. A provider error or timeout is a failed test. Settings do not load `.env` automatically, so use `uv run --env-file .env` for the server and integration checks. Exported variables take precedence over `.env`. Restart the server after changing settings.

## Run the local API

In one terminal, start the server with the exported paths and NVIDIA key:

```sh
cd /home/lakshya/VoxOps
uv run --env-file .env voxops
```

Keep it running. In a second terminal, check the service:

```sh
curl -fsS http://127.0.0.1:8000/health | jq
curl -fsS http://127.0.0.1:8000/ready | jq
```

Expect a health response and `ready`, `storage`, and `workspaces` set to true. Readiness does not test NVIDIA or GitHub. Open `http://127.0.0.1:8000/` for the browser API tester; it covers Stage 1. The API schemas are at `http://127.0.0.1:8000/docs`.

Analyze the fixture without a model, then with the configured model:

```sh
curl -fsS http://127.0.0.1:8000/api/v1/repositories/analyze \
  -H 'Content-Type: application/json' \
  -d '{"path":"/home/lakshya/VoxOps/tests/fixtures/fastapi_only","use_llm":false}' | jq '{detected_units, ai_usage, deterministic_only, warnings}'
curl -fsS http://127.0.0.1:8000/api/v1/repositories/analyze \
  -H 'Content-Type: application/json' \
  -d '{"path":"/home/lakshya/VoxOps/tests/fixtures/fastapi_only","use_llm":true}' | jq '{detected_units, ai_usage, deterministic_only, warnings}'
```

Both responses should identify a FastAPI unit. The first skips the model. In the second, inspect `ai_usage.status`: `used` confirms a model response, while `failed` or `unavailable` means deterministic fallback, not a successful model integration.

Test transcript input and the unconfigured Gnani boundary:

```sh
curl -fsS http://127.0.0.1:8000/api/v1/voice/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"mode":"transcript","text":"analyze this repository","language":"en-IN"}' | jq
curl -sS -i http://127.0.0.1:8000/api/v1/voice/transcribe \
  -H 'Content-Type: application/json' -d '{"mode":"gnani"}'
```

The first returns the supplied text. The second returns HTTP 503 and `gnani_not_configured`. Live Gnani transcription and cloud deployment are not implemented.

## Test a Stage 1 worktree

Create a disposable committed source repository. Only committed files appear in its worktree; local edits are omitted.

```sh
mkdir /tmp/voxops-integration-tests/worktree-source
cp tests/fixtures/fastapi_only/main.py tests/fixtures/fastapi_only/requirements.txt /tmp/voxops-integration-tests/worktree-source/
git -C /tmp/voxops-integration-tests/worktree-source init -b main
git -C /tmp/voxops-integration-tests/worktree-source add main.py requirements.txt
git -C /tmp/voxops-integration-tests/worktree-source -c user.name=Tester -c user.email=tester@localhost commit -m 'Create fixture'
curl -fsS http://127.0.0.1:8000/api/v1/worktrees -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/voxops-integration-tests/worktree-source"}' | tee /tmp/voxops-integration-tests/worktree.json | jq
WORKTREE_ID=$(jq -r '.id' /tmp/voxops-integration-tests/worktree.json)
curl -fsS "http://127.0.0.1:8000/api/v1/worktrees/$WORKTREE_ID" | jq
curl -fsS -X DELETE "http://127.0.0.1:8000/api/v1/worktrees/$WORKTREE_ID" | jq
```

Inspect the location and source commit before deletion. A clean worktree is removed with its generated branch. If you change it, inspect the changes before explicitly using `?discard_changes=true`. The source branch and source files remain untouched.

## Configure GitHub access

The hosted GitHub MCP endpoint is `https://api.githubcopilot.com/mcp/`. Use a user-managed GitHub personal access token with access to the disposable repository and permissions needed to read repository metadata and create and write a private repository. VoxOps needs `get_me`, `search_repositories`, `create_repository`, and, when search omits push permissions, `list_repository_collaborators`. GitHub's available toolsets and token permissions determine what the endpoint exposes. See [GitHub's MCP setup](https://github.com/github/github-mcp-server#remote-github-mcp-server) and [server configuration](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md).

For a straightforward personal-account test, open GitHub Settings, Developer settings, Personal access tokens, Tokens (classic), then Generate new token (classic). Set an expiry and select `repo` and `workflow`. The `repo` scope grants private repository access; `workflow` permits publishing the generated CI workflow. These scopes are broad, so use a test account if you need isolation. Organization policies may restrict classic tokens. GitHub documents the [scope meanings](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps).

In the server terminal, stop the server and configure the token without printing it:

```sh
export VOXOPS_GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/
read -r -s -p 'GitHub MCP token: ' VOXOPS_GITHUB_MCP_TOKEN; printf '\n'
export VOXOPS_GITHUB_MCP_TOKEN
uv run --env-file .env python scripts/check_integrations.py --github
```

Expect the authenticated identity and discovered tool count. This probe does not create a repository. Fix missing tools or permissions before continuing.

Git over HTTPS needs separate authentication. Authenticate `gh` interactively with an account that can access the test repository. Create a trusted helper outside the project, using the actual absolute path to `gh`:

```sh
gh auth login --hostname github.com --git-protocol https --scopes repo,workflow
GH_BIN=$(command -v gh)
case "$GH_BIN" in /*) ;; *) echo 'gh must have an absolute path' >&2; exit 1;; esac
mkdir -p /tmp/voxops-test-tools
printf '#!/bin/sh\nexec "%s" auth git-credential "$@"\n' "$GH_BIN" > /tmp/voxops-test-tools/gh-credential-helper
chmod 700 /tmp/voxops-test-tools/gh-credential-helper
export VOXOPS_GIT_CREDENTIAL_HELPER=/tmp/voxops-test-tools/gh-credential-helper
```

The helper must be the wrapper, not the `gh` executable itself. VoxOps clears global Git helper configuration, so `gh auth setup-git` alone is insufficient. For a pre-existing disposable repository with a `main` branch, test both MCP metadata and Git's remote SHA without pushing:

```sh
uv run --env-file .env python scripts/check_integrations.py --github --repository OWNER/NAME
```

Replace `OWNER/NAME` with that repository. Use a private repository to exercise Git credentials, since public repositories may allow anonymous reads. The check uses a temporary local Git directory. It reports the lookup and remote SHA without pushing. A null SHA means no `main` branch was found. Investigate errors before publishing. Restart `uv run --env-file .env voxops` in the same terminal so the server receives both GitHub settings and the NVIDIA key.

## Verify and publish a disposable GitHub source

Use a small runnable FastAPI, React/Vite, or Next.js repository you own. Do not use
the scan-only fixture. Start with a private disposable repository and inspect every
review before authorizing publication.

Create a workflow using your source URL and a new client key:

```sh
curl -fsS http://127.0.0.1:8000/api/v1/workflows \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(date +%s)-disposable-test" \
  -d '{"repository_url":"https://github.com/OWNER/REPOSITORY","ref":"main"}'
```

Replace `OWNER/REPOSITORY` first. Creation returns HTTP 202 and a `workflow_id`.
Open the API tester at `http://127.0.0.1:8000/`, enter that workflow ID, and poll its
status. The tester also supports workflow creation directly.

If the workflow waits for environment values, load its environment form, enter only
missing required values, and submit. Values stay in memory. The page acknowledges
the form through the workflow resume endpoint.

If the workflow waits for approval, load and inspect the current review. Approve or
reject that exact review. Source approval and final artifact approval are separate
prompts when both are needed. Final approval requires passed Docker checks.

A successful result reports `ready_for_deployment` with matching local and remote
commit SHAs. It removes temporary containers, worktrees, and the source checkout.
MongoDB retains evidence. The result does not deploy an application.

To stop the attempt, use the workflow cancel button. For another attempt, create a
new workflow with a new client key. See the [workflow API examples](../README.md)
for polling, environment submission, and exact review resume requests.
