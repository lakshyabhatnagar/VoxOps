# VoxOps

Stage 4 runs repository acquisition, cached framework analysis, artifact generation, local Docker
verification, targeted repair, human review and approved Git publication through LangGraph.
MongoDB holds records, artifact contents and official LangGraph checkpoints. Application source,
worktrees and build inputs stay in temporary directories. The result is a verified branch and SHA
ready for deployment. Cloud deployment is outside this stage.

See [Stage 4 decisions](docs/stage4-decisions.md) and
[test evidence and limitations](docs/stage4-verification.md).

The Python package and CLI are `voxops`; service settings use the `VOXOPS_` prefix.
Operational records and generated artifact bytes now live in MongoDB. Old developer JSON records are not imported. Temporary source checkouts and worktrees remain on disk.

Local repository intelligence for a future voice-first deployment assistant. Stage 1 scans
FastAPI, React/Vite and Next.js projects, detects independent monorepo units, runs a bounded
LangGraph pipeline with optional typed PydanticAI topology assistance, and manages safe temporary
Git worktrees. It does not deploy services or execute project install/build commands.

Requires Python **3.11+**, MongoDB, Git for worktrees, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The checked-in `uv.lock` pins direct and transitive dependencies. Verified locally with Python 3.14.

```sh
# Development MongoDB. Keep the port private and retain its database volume.
docker run -d --name voxops-mongo -p 127.0.0.1:27017:27017 -v voxops-mongo:/data/db mongo:8
uv sync --locked
uv run --env-file .env voxops
```

The server binds **127.0.0.1:8000**.

Open **http://127.0.0.1:8000/** to start a GitHub workflow, poll progress, supply missing environment
values and inspect/approve a review. Existing local development scan/worktree tools remain private.
The full response JSON is expandable. No frontend install or build step is needed.

FastAPI startup detection prefers a scoped `Procfile` command, then a README shell-code example
matching a detected app, then a final-stage Docker startup command, and finally source inference.
It preserves supported runners and server flags, recognizes exported factory results such as
`app = create_app()`, and reports documented Alembic migrations in `runtime.prerequisite_commands`.
The tester displays these prerequisites before the startup command. Commands are metadata only;
scanning never installs dependencies, applies migrations or starts the target app. Only a narrow
set of commands/options is read; arbitrary scripts and complex shell expressions are not interpreted.
Conflicting examples in the selected source are reported as a question.

For command-line testing, in another terminal:

```sh
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
uv run python scripts/demo_stage1.py "$PWD/tests/fixtures/monorepo_fastapi_vite" \
  --transcript "analyze this repository"
```

Direct analyze request (substitute an allowed absolute local path):

```sh
curl -s http://127.0.0.1:8000/api/v1/repositories/analyze \
  -H 'Content-Type: application/json' \
  -d '{"path":"/absolute/path/to/workspace","use_llm":false}'
```

Response excerpt (IDs and timestamps vary):

```json
{
  "schema_version": "1",
  "scan": {"id": "generated-scan-id", "git": {"status": "uninitialized"}},
  "detected_units": [
    {"root_path":"apps/api","framework":"fastapi","role":"http_service"},
    {"root_path":"apps/web","framework":"react_vite","role":"static_frontend"}
  ],
  "dependencies": [],
  "deterministic_only": true,
  "ai_usage": {"status":"unavailable","requests":0}
}
```

This is an excerpt, not the full response schema. Full typed schemas are on `/docs`.

Configure settings with environment variables; `.env.example` contains names/placeholders only:
Settings do not load `.env` automatically. Use `uv run --env-file .env voxops` to load it.

| Setting | Default / meaning |
| --- | --- |
| `VOXOPS_ALLOWED_ROOTS` | JSON array of existing directories; default is current directory |
| `VOXOPS_MONGO_URI` | `mongodb://127.0.0.1:27017`; keep credentials private |
| `VOXOPS_MONGO_DATABASE` | `voxops`; canonical records and generated artifacts |
| `VOXOPS_MAX_DEPTH` | 8 directory levels |
| `VOXOPS_MAX_FILES` | 3000 total directory entries visited |
| `VOXOPS_MAX_FILE_BYTES` | 1048576 bytes per targeted manifest/source/env file |
| `VOXOPS_MAX_CONTEXT_CHARS` | 64000 characters of sanitized model JSON, configurable up to 128000 |
| `VOXOPS_MODEL_TIMEOUT` | 20 seconds per optional specialist call |
| `VOXOPS_ARTIFACT_MODEL_TIMEOUT` | 180 seconds per Stage 2 generation or repair request, configurable up to 300 |
| `VOXOPS_MODEL` | `anthropic:claude-sonnet-4-5`; replaceable provider/model string |
| `ANTHROPIC_API_KEY` | Optional secret, environment only |
| `OPENAI_API_KEY` | Optional alternative provider secret, environment only |
| `NVIDIA_API_KEY` | Optional NVIDIA secret; use `nvidia:mistralai/mistral-large-4-1t-a52b` |
| `GNANI_API_KEY` | Reserved; does not enable unverified live transport |

No model credential is required for deterministic scans or readiness. A configured model is called
only for uncertainty or `use_llm=true`; failures return deterministic findings plus warnings. Model
access is optional and may incur the configured provider's charges. Stage 1 does not send source secrets or raw source as model inputs. `VITE_` and `NEXT_PUBLIC_` configuration names are marked browser-exposed
build-time configuration; this is exposure metadata, not permission to put secrets in browser code.

Run the high-value offline tests and lint:

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/check_comments.py
# Optional browser-script request checks (requires Node.js):
node tests/test_api_tester.mjs
```

Worktree demo for an already committed local repository within configured allowed roots:

```sh
uv run python scripts/demo_stage1.py /absolute/path/to/committed/repository --worktree
```

Git `uninitialized`, `missing_initial_commit`, `detached_head` and nested-root conditions receive
explicit prerequisite errors. Dirty source repositories are supported with a warning and contain
committed content only in the new worktree. Dirty temporary worktrees cannot be removed without
the explicit `discard_changes=true` cleanup policy.

**Gnani live STT is not configured.** Official current and legacy documentation was reviewed;
no account-specific contract or credential was supplied. The transcript stub works, and the live
adapter returns a typed configuration error. See [contract decision and sources](docs/decisions.md).

See [API/harness usage](docs/stage1-usage.md) and [architecture](docs/architecture.md). This API is a
trusted single-process local tool, not a public server or a security sandbox. Scan bounds and
conservative syntax matching can leave dynamic framework settings unresolved. MongoDB retains domain records. Stage 4 workflow checkpointing and hosted GitHub input are under implementation; the existing private development routes remain available until replaced.

Historically, Stage 2 added artifact planning, bounded generation and validation, review, and GitHub publication.
Its historical acceptance checks covered offline generation and loopback HTTP/Git publication.
Stage 2 did not perform runtime verification or deploy applications. Current Docker evidence is in
[Stage 3 verification](docs/stage3-verification.md); live GitHub/model calls and GitHub CI execution
are not claimed by these local tests. See [Stage 2 usage](docs/stage2-usage.md),
[architecture](docs/stage2-architecture.md), [decisions](docs/stage2-decisions.md),
[security](docs/stage2-security.md), and [verification](docs/stage2-verification.md).

For NVIDIA model setup, GitHub authentication, and a complete disposable publishing exercise,
follow [integration testing](docs/integration-testing.md). The sample environment selects NVIDIA
with a 60-second timeout and one generation worker. Deterministic scans still work without a key.

Deferred work includes cloud selection and deployment, provider adapters, hosted persistence,
Clerk, billing, VS Code UI, and production speech navigation.

See the [completion report](docs/stage1-verification.md) for the final project tree, requirement
evidence, exact verification commands and observed results.

## Run a GitHub workflow

Configure the model, GitHub MCP and trusted Git HTTPS authentication described in
[integration setup](docs/integration-testing.md). Supply a source whose tracked
history satisfies the existing path, secret and size checks. No credentials belong
in the repository URL or request body.

```sh
curl -s http://127.0.0.1:8000/api/v1/workflows \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: unique-request-0001' \
  -d '{"repository_url":"https://github.com/OWNER/REPOSITORY","ref":"main"}'
curl -s http://127.0.0.1:8000/api/v1/workflows/WORKFLOW_ID
```

Creation returns HTTP 202 immediately. Poll every three seconds. A second active
workflow returns `server_busy`; there is no queue. Repeating an idempotency key
with the same request returns the same workflow. Optional `strategies` maps a
unit ID or relative application root to `container`, `native` or `static`.
`owner` and `name` together choose a different destination when needed.

At `waiting_for_env`, load `/api/v1/artifact-runs/RUN_ID/verification/env-form`
and submit its digest and field-ID/value mapping to `.../env-values`. Then POST
`{"schema_digest":"DIGEST"}` to `/api/v1/workflows/WORKFLOW_ID/resume`.
Values stay in memory and never enter the resume command or checkpoint.

At `waiting_for_approval`, inspect `/api/v1/artifact-runs/RUN_ID/review`. Resume
with the exact `review_id`, `digest` and `decision` (`approve` or `reject`). The
same workflow/thread continues. A rejected/stale review does not publish.
`ready_for_deployment` includes the verified remote branch and SHA. All required
local checks must pass; this workflow has no incomplete-verification waiver.

POST `.../cancel` to stop and clean up. To start another attempt, create a new workflow
with a new idempotency key. Expired or removed source cannot be reconstructed from a
checkpoint. Scans, run histories, reviews and results remain readable after cleanup.

Defaults are 45 minutes of accumulated active work, 30 minutes per human-input
pause and 75 minutes of absolute workspace lifetime. Configure
`VOXOPS_ACTIVE_EXECUTION_TIMEOUT_MINUTES`, `VOXOPS_HITL_IDLE_TIMEOUT_MINUTES` and
`VOXOPS_WORKSPACE_MAX_LIFETIME_MINUTES`. `VOXOPS_MAX_REPAIR_CYCLES=1` limits each
failing unit per user attempt. `VOXOPS_LLM_CALL_RETRIES=1` permits one extra transient
model call within the existing three-content-attempt allowance. Global model
spending caps and historical request records remain enforced.

Use one worker and loopback binding. For a private VM, access through an operator
SSH tunnel, for example `ssh -L 8000:127.0.0.1:8000 user@vm`. Public authentication,
billing and cloud deployment are later stages. After restart, running records are
marked `interrupted` and require explicit eligible resume. Lost temporary secrets
need re-entry and verification; expired workspaces require a new source attempt.
After an abrupt stop, the unrecorded interval until restart counts conservatively
toward the active-time budget. A restart cannot reset that allowance.

The old `/artifact-runs/{id}/execute` and `/publish` routes and their CLI demos
have been removed. Old developer JSON records are not automatically imported.
