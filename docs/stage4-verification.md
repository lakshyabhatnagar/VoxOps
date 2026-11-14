# Stage 4 verification

Baseline is `cd6c9f8` on `stage3/sandboxed-local-execution`. No project commit,
push, merge, or deployment was performed. The working changes remain uncommitted.

## Storage and analysis

- Migrated Stage 1–3 regression checkpoint passed with `361 passed, 1 skipped`.
- Real persistence check passed with `2 passed`:

```sh
VOXOPS_TEST_MONGO_URI=mongodb://127.0.0.1:27018 \
  .venv/bin/pytest tests/test_mongo_storage.py -q
```

The real test checks scan, run, independent task, blob, plan, source and worktree
records, corruption rejection, and native interrupt/resume through a second
MongoDB client and official `MongoDBSaver`. There is no operational JSON fallback.

Cache checks use actual Git commits and verify exact-commit reuse, content reuse
across irrelevant documentation commits, README startup and manifest invalidation,
model/config invalidation, fresh scan identity, current SHA, and a new checkout
path after the original checkout was deleted. Fast tests inject `mongomock` only
at the test boundary.

## Workflow checks and focused review

The final focused command passed with `47 passed`:

```sh
.venv/bin/pytest tests/test_stage4_graph.py tests/test_workflow_api.py \
  tests/test_workflow_attempts.py tests/test_stage2_git.py -q
```

Coverage includes environment/source/final-review native interrupts, rejection,
stale approval, local publication, asynchronous create, duplicate keys, a busy VM,
cancellation, active/idle/absolute expiry, and startup interruption without replay.
The reducer rejects conflicting unit evidence. Old execute and publish HTTP
routes are absent. New workflows retain per-file attempt histories and bounded
repair allowances.

Focused pstack review reproduced and corrected these issues before final regression:

- Cancellation rejected an operation owned by a dead process. Recovery now releases
  that claim before cleanup.
- A hard restart could lose the latest active-time interval. The start timestamp
  is persisted; the uncertain interval counts conservatively until restart.
- Idle expiry reported the wrong reason. Idle, active and workspace expiry now
  have separate error categories.
- A provider connection error was treated as invalid model output. Recognized
  OpenAI/Anthropic connection errors now receive one bounded transport retry.
- Several storage/Git expressions still ran before entering a worker thread.
  The blocking operations now run inside that thread.

Additional tests verify acquisition cancellation joins its worker and removes the
finished clone. Browser-script tests require loading the exact current review
before approving and stop polling while a person reads it.

## Real end-to-end smoke

This opt-in test passed with `1 passed` in 498.71 seconds:

```sh
VOXOPS_TEST_MONGO_URI=mongodb://127.0.0.1:27018 \
VOXOPS_TEST_REAL_DOCKER=1 \
  .venv/bin/pytest tests/test_stage4_real_pipeline.py -q -s --maxfail=1
```

Actual integrations were MongoDB, the official checkpointer, Git acquisition from
a disposable bare remote, Docker builds, running containers, HTTP probes, and
approved Git publication to that local remote. GitHub URL transport/metadata and
model generation/repair responses were explicit fixtures.

Both FastAPI and React/Vite built and answered HTTP probes. An intentionally broken
FastAPI Dockerfile failed the actual build, triggered one targeted repair, and
passed reverification. Environment and final review paused through native
interrupts and resumed on the same thread. Source HEAD/content stayed unchanged.
All owned application resources, worktrees and clones were removed. BSON documents
and checkpoints contained none of the submitted secret value. A separate Python
process reopened MongoDB and read the completed workflow and both unit outcomes.

Recorded workflow was `fec00ae65c1a45139158c60435271625`. The verified remote SHA
was `abd570e9be10c34ca472424efd0b2f4bb4b64cb9`. Unit `713194d2bffaba1d` consumed
one repair cycle. `cleanup_complete` was true. These IDs are smoke-test evidence,
not an operator database to reconnect to; the test dropped its unique database.

## Final regression

- `.venv/bin/pytest -q --maxfail=3` passed with `387 passed, 2 skipped` in
  141.90 seconds. The two skips are the opt-in real MongoDB and real Docker tests,
  which passed separately above. Warnings are existing SDK deprecations.
- `.venv/bin/ruff check .` passed.
- `node tests/test_api_tester.mjs` passed, including workflow polling and exact
  displayed-review approval. `node tests/test_environment_form.mjs` passed.
- `.venv/bin/python scripts/check_comments.py` and `git diff --check` passed.
  New class/function explanations were inspected separately because that script
  validates Python syntax rather than enforcing comment counts.
- The disposable `voxops-stage4-mongo` container was stopped after testing.
  Successful smoke already verified removal of its application containers,
  owned source, worktree and build resources. No preexisting container was changed.

The first final-suite launch did not start because automatic permission review
reached its deadline. Its permitted retry ran successfully; this was an approval
service timeout, not a test failure.

## MVP limits

- No live model, GitHub MCP, or internet GitHub publication was claimed by this
  smoke. It used controlled transport/metadata and typed model responses.
- Public Microsoft FastAPI acquisition stopped at the existing publisher guard
  against tracked `.devcontainer` and `.vscode` paths. Another attempted public
  fixture failed transport. Repositories containing unsupported protected paths
  still stop rather than weakening those guards.
- Docker scheduling remains inside the existing dependency-aware runner. Graph
  `Send` workers merge its saved unit receipts; they do not run Docker again.
- Real Docker smoke covered FastAPI and React/Vite. Existing Next.js adapter tests
  remain in regression; this task did not repeat a real Next.js image build.
- Browser-script checks use Node's VM, not a full browser. Probes check local
  startup/readiness, not complete application behavior or external provisioning.
- One process and one worker are required. Access remains loopback/private.
  There is no public authentication, billing, queue, or cloud deployment.
- Temporary secrets are lost on restart. Eligible explicit resume still requires
  valid source/configuration authority. Deleted or expired workspaces require a
  new workflow. The optional retry endpoint is omitted to avoid shared ownership
  of a paused run; a new creation key starts a new workflow.
- Old JSON records are intentionally not imported. MongoDB must be available.
