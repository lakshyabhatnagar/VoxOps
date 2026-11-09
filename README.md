# VoxOps

Stage 3 verifies the unpublished Stage 2 candidate through local Docker before final approval and
publication. It includes one required-environment form, bounded repair, and explicit acknowledgement
of incomplete external-service checks. It does not deploy to the cloud.
See [usage](docs/stage3-usage.md), [architecture](docs/stage3-architecture.md),
[environment resolution](docs/stage3-env-resolution.md), [security](docs/stage3-security.md),
[decisions](docs/stage3-decisions.md), and [verification](docs/stage3-verification.md).

The Python package and CLI are `voxops`; service settings use the `VOXOPS_` prefix.
Previous development runtime records are preserved unchanged in `.voxops/archive/legacy`.

Local repository intelligence for a future voice-first deployment assistant. Stage 1 scans
FastAPI, React/Vite and Next.js projects, detects independent monorepo units, runs a bounded
LangGraph pipeline with optional typed PydanticAI topology assistance, and manages safe temporary
Git worktrees. It does not deploy services or execute project install/build commands.

Requires Python **3.11+**, Git for worktrees, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The checked-in `uv.lock` pins direct and transitive dependencies. Verified locally with Python 3.14.

```sh
uv sync --locked
uv run voxops
```

The server binds **127.0.0.1:8000**.

Open **http://127.0.0.1:8000/** for the minimal API tester: analyze a local path, test transcripts,
check health/readiness, and create, inspect or clean up worktrees using forms and readable results.
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
| `VOXOPS_DATA_DIR` | `.voxops`, private atomic JSON result/worktree records |
| `VOXOPS_MAX_DEPTH` | 8 directory levels |
| `VOXOPS_MAX_FILES` | 3000 total directory entries visited |
| `VOXOPS_MAX_FILE_BYTES` | 131072 bytes per targeted manifest/source/env file |
| `VOXOPS_MAX_CONTEXT_CHARS` | 16000 characters of sanitized model JSON |
| `VOXOPS_MODEL_TIMEOUT` | 20 seconds per optional specialist call |
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
conservative syntax matching can leave dynamic framework settings unresolved. Local JSON results
are not durable LangGraph checkpoints or production crash-resume support.

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
