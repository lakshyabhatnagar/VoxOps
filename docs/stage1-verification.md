# Stage 1 completion report

Historical Stage 1–3 evidence below describes the original implementation. Stage 4
replaces operational JSON files and filesystem locks with MongoDB records and a
single-process execution boundary. See [Stage 4 decisions](stage4-decisions.md).

Verified on 2026-11-02 against `GOAL_STAGE_1.md`, using Python 3.14.8 and Git 2.56.0.
The starting workspace contained the goal document and environment-managed empty metadata directories.
No original source code existed or was removed. Implementation did not initialize, commit or connect
the root workspace to a remote. Final Git inspection reports an unborn `main` branch, no commits,
no remotes, and the deliverable files untracked. All commits used in verification were in disposable
test fixtures. Root worktree creation therefore still needs an initial commit supplied by the user.

## Requirements and observed evidence

| Deliverable / acceptance gate | Current implementation and verification |
| --- | --- |
| Typed local FastAPI service, errors and readiness | `main.py`, `core/`, `api/`; API integration tests and a real Uvicorn process returned HTTP 200 for `/health` and `/ready`. Ready reported storage/workspaces true and Gnani live false. |
| Bounded deterministic scanner and Git metadata | `repository/`; tests cover traversal, symlink escape, entry/size bounds, oversized files, env-value sentinels, no source execution and uninitialized Git. |
| Independent framework adapters and monorepos | `frameworks/`; five checked-in fixtures passed exact classification checks both through direct scanning and the running HTTP service. Tests also cover explicit Next export conflicts, commented-out config, Vite CLI output/SSR overrides and FastAPI factories. |
| Typed stable contracts | `domain/models.py`; Pydantic models reject extra fields and serialize validated aggregates. Tests compare persisted JSON with complete returned results and check stable unit IDs across rescans. |
| Optional PydanticAI specialist | `agents/` and `analysis/`; actual `Agent` with `TestModel` exercises structured output, role refinement, deterministic precedence, invalid-output fallback, capped context and request accounting. No paid model call was made. |
| Fixed LangGraph pipeline | `workflows/stage1_graph.py`; tests invoke the compiled graph end-to-end, verify no-key operation and model fallback, and preserve policy/storage failure categories. Fixed conditional maps stop failed stages. |
| Worktree lifecycle and source safety | `repository/worktree_manager.py`; real Git tests verify branch/HEAD/status/bytes preservation, omitted local edits, owned create/list/inspect/remove, dirty/new-commit protection, no-HEAD/detached/nested prerequisites, collisions, hook/filter suppression and interrupted cleanup recovery. |
| Honest Gnani boundary and transcript mode | `voice/`; stub returns typed ordinary text, live adapter raises `GnaniNotConfigured`, obvious credential text is rejected. The real API returned HTTP 503 with `gnani_not_configured`. No guessed transport was implemented. |
| Developer harness without cloud access | `scripts/demo_stage1.py`; exercised against a fresh API process and a disposable committed FastAPI/Vite fixture. Transcript, analyze, create, inspect and cleanup all succeeded; source HEAD/status remained unchanged. |
| Setup, dependencies, docs, tests and comment rule | `pyproject.toml`, `uv.lock`, `.env.example`, README, docs and tests. Offline lock validation resolved 65 packages. Ruff passed. All 144 Python function/class definitions passed the immediate two-line Why/How check; the sole authored TS route function also has those two comment lines. |

Actual live classifications:

| Fixture | Units / roles |
| --- | --- |
| `fastapi_only` | `.` → `http_service` |
| `vite_only` | `.` → `static_frontend` |
| `nextjs_fullstack` | `.` → `fullstack_web` (one unit) |
| `monorepo_fastapi_vite` | `apps/api` → `http_service`; `apps/web` → `static_frontend` |
| `monorepo_shared_package` | `apps/web` → `static_frontend`; shared library excluded and recorded as a shared package |

All live fixture requests used `use_llm=true` without credentials and returned deterministic-only
results with zero model requests. Evidence and uncertainty remain explicit; no producer/consumer
deployment link is invented from a URL-shaped env key. Model inputs contain only bounded safe facts
and relative paths. Ambient LangSmith tracing is disabled for the analysis graph.

## Commands and outcomes

Commands actually used from the repository root:

```sh
python -m venv .venv
.venv/bin/python -m pip install uv
.venv/bin/uv lock
.venv/bin/uv sync --locked --cache-dir /tmp/voxops-uv-cache
.venv/bin/uv lock --check --offline --cache-dir /tmp/voxops-uv-cache
.venv/bin/ruff check src tests scripts
.venv/bin/python scripts/check_comments.py
.venv/bin/pytest -q
.venv/bin/voxops
```

Final suite: **23 passed, 13 warnings in 3.85 seconds**. The warnings are dependency-level
LangSmith calls to deprecated `asyncio.iscoroutinefunction` on Python 3.14. Ruff: **all checks
passed**. Documentation gate: **144 definitions across 37 Python files passed**. Lock validation:
**65 packages resolved, unchanged**.

Dependency downloads and API tests required the execution environment's elevated network access.
The restricted sandbox stalled the ASGI client's thread wakeup; the same suite completed in under
four seconds outside that sandbox. No live Anthropic, OpenAI, Gnani or cloud-provider account was
used. The real HTTP smoke test started the service, checked all five fixtures, exercised the harness
and terminated the server cleanly. No background API is left running by verification.

Working commands in the current prepared workspace:

```sh
# Terminal 1
.venv/bin/voxops

# Terminal 2
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
.venv/bin/python scripts/demo_stage1.py "$PWD/tests/fixtures/monorepo_fastapi_vite" \
  --transcript "analyze this repository"

# Requires an existing committed repository inside configured allowed roots:
.venv/bin/python scripts/demo_stage1.py /absolute/path/to/committed/repository --worktree
```

The final real-process worktree smoke used that last harness command against a disposable fixture
created under `.voxops`, then removed the fixture after verifying source HEAD/status. Runtime
scan records and removed-worktree metadata remain in the ignored local data directory.

## Gnani, limitations and deviations

Live Gnani remains **explicitly not configured**, as allowed by the goal. The account's verified
current contract and credentials are needed before implementing or smoke-testing a real transport.
Official source verification and the distinction between current Prisma v3 and legacy API-service
documentation are recorded in [decisions.md](decisions.md). Stub mode is functional and labeled.

Optional live model providers were not called; their integration is tested using PydanticAI's
structured offline model. Dynamic JS/Python configuration may still require manual review.
Bounded scanning can omit files and reports truncation warnings. The local single-process registry
is not a multi-worker service, durable LangGraph checkpoint system or production crash-resume.
Do not run it as a public/proxied filesystem API. Worktrees are isolation, not execution sandboxes.

The layout consolidates the proposed multiple domain, route and graph-node files to avoid artificial
indirection. Security, framework, specialist, voice and storage boundaries remain separate. This is
the only structural deviation; it is permitted by the proposed-layout guidance.

Deferred exactly as requested: cloud service ranking/selection/deployment/authentication, provider
MCP adapters, artifact/CI/Dockerfile generation, repair workers and retries, Docker sandboxing,
GitHub publishing, Clerk, billing/quotas, MongoDB migration, hosted supervisors, production voice
navigation, VS Code UI, wake words and TTS. No Stage 2+ operation was attempted.

## Final authored project tree

Runtime environments, caches, ignored result files and environment-managed empty metadata
directories are omitted.

```text
.
├── GOAL_STAGE_1.md                    # original requirement document, preserved
├── README.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
├── docs/
│   ├── architecture.md
│   ├── decisions.md
│   ├── stage1-usage.md
│   └── stage1-verification.md
├── scripts/
│   ├── check_comments.py
│   └── demo_stage1.py
├── src/voxops/
│   ├── __init__.py
│   ├── main.py
│   ├── agents/topology_agent.py
│   ├── analysis/topology_resolver.py
│   ├── api/
│   │   ├── routes.py
│   │   └── schemas.py
│   ├── core/
│   │   ├── errors.py
│   │   ├── logging.py
│   │   └── settings.py
│   ├── domain/models.py
│   ├── frameworks/
│   │   ├── base.py
│   │   ├── fastapi.py
│   │   ├── nextjs.py
│   │   ├── react_vite.py
│   │   └── registry.py
│   ├── repository/
│   │   ├── env_inspector.py
│   │   ├── filesystem_scanner.py
│   │   ├── git_inspector.py
│   │   ├── manifest_reader.py
│   │   ├── path_policy.py
│   │   └── worktree_manager.py
│   ├── storage/scan_store.py
│   ├── voice/
│   │   ├── base.py
│   │   ├── gnani.py
│   │   └── transcript_stub.py
│   └── workflows/stage1_graph.py
└── tests/
    ├── conftest.py
    ├── test_api.py
    ├── test_framework_detection.py
    ├── test_repository_safety.py
    ├── test_stage1_graph.py
    ├── test_voice_contract.py
    ├── test_worktree_manager.py
    └── fixtures/
        ├── fastapi_only/
        │   ├── .env.example
        │   ├── main.py
        │   └── requirements.txt
        ├── vite_only/
        │   ├── .env.example
        │   ├── index.html
        │   ├── package.json
        │   └── vite.config.ts
        ├── nextjs_fullstack/
        │   ├── .env.example
        │   ├── package.json
        │   └── app/api/hello/route.ts
        ├── monorepo_fastapi_vite/
        │   ├── package.json
        │   ├── pnpm-lock.yaml
        │   ├── pyproject.toml
        │   └── apps/
        │       ├── api/
        │       │   ├── main.py
        │       │   └── pyproject.toml
        │       └── web/
        │           ├── index.html
        │           └── package.json
        └── monorepo_shared_package/
            ├── package.json
            ├── apps/web/
            │   ├── index.html
            │   └── package.json
            └── packages/shared/
                ├── package.json
                ├── vite.config.ts
                └── src/index.ts
```
