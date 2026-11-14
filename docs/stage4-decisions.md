# Stage 4 implementation decisions

Baseline is `cd6c9f8` on `stage3/sandboxed-local-execution`, rather than the
`main` revision named in the brief. Only `goal.md` was untracked at entry.

## Existing call map

`main.create_app` creates `ScanStore`, `Stage1Workflow`, `WorktreeManager`, and
`ArtifactService`. Stage 1 scans, detects units, optionally resolves uncertainty,
and persists `AnalysisResult`. Stage 2 plans tasks and runs `Stage2Graph` with
`ArtifactGenerator`. At baseline, `ArtifactService.execute/_verify` owned candidate
application, environment preparation, `ExecutionRunner`, repairs, and review.
`GitPublisher` owns approved publication and remote SHA checks.

## Replacement boundaries

1. Keep the active `ScanStore` and `ArtifactStore` interfaces where useful, but
   replace their JSON implementation with MongoDB collections. Remove task-file
   access from `ArtifactGenerator`, plan-file access from `ArtifactService`, and
   the JSON registry from `WorktreeManager`. Tasks become the canonical ledger;
   assembled runs read that ledger rather than keeping a competing copy.
2. Keep synchronous domain methods behind one PyMongo adapter. Graph coordination
   runs synchronous storage/Git work on worker threads. One process-local lock
   protects project mutation; the workflow manager owns the single execution slot.
3. Use the maintained `MongoDBSaver` and its asynchronous methods. Inspect the
   installed package rather than implement a saver or use a deprecated API.
4. Acquire supported GitHub sources into disposable directories. Keep immutable
   commit identity separate from reusable content identity. README startup evidence
   participates in analysis identity. Refresh scan/path/revision metadata on hits.
5. Extract generation, preparation, verification, repair, and review from the
   existing service. Preserve Docker mechanics, journals, input checks, bounded
   attempts, secret sessions, and publication checks. Delete the replaced HTTP
   executor when the supervisor becomes the product entry point.
6. Keep graph state to IDs and small unit outcomes. Native interrupts own pauses.
   Environment values enter only the volatile environment manager.

## Checkpoints and evidence

Follow CP0 through CP6 in `goal.md`, verifying each before advancing. Fast tests
use an injected Mongo mock; persistence/restart/checkpointer evidence uses a real
disposable MongoDB. Final smoke uses real Docker and a disposable Git remote.
No automatic import of old developer JSON records. No cloud deployment or push
to a user's application repository during tests.

## Execution scheduling compromise

Keep `ExecutionRunner` as the only dependency scheduler. It already verifies independent
units concurrently, checks toolchain/input digests, owns prerequisite ordering and
cleans Docker resources. The Stage 4 graph checkpoints before/after this runner batch,
then merges small unit receipts through `Send` workers and a conflict-checking reducer.
Repair is a separate sequential graph step and re-enters the same runner, preserving
passed unit reuse. This deliberately avoids a second scheduler or duplicated Docker
logic. A worker receipt is reporting persisted evidence, not an additional Docker run.

`generate_candidate`, `verify_candidate` and `repair_candidate` are the extracted
service boundaries. The old development `execute` adapter and write endpoints are removed. Prior-stage
regression tests call extracted domain steps; the new workflow suite tests HTTP orchestration. MongoDB and Git
operations in asynchronous service/generator/publisher paths run on worker threads.
Execution callbacks share one async writer lock before saving coherent run updates.

The official [MongoDB LangGraph integration](https://www.mongodb.com/docs/atlas/ai-integrations/langgraph/)
uses `MongoDBSaver`. Installed version 0.5.1 supplies its maintained async methods.

## Lifecycle and public commands

Workflow creation, polling, resume and cancellation use `/api/v1/workflows`.
The optional retry endpoint is omitted. A new creation key starts a new workflow
and source checkout; it does not transfer a paused run to another thread.
The generator's new-attempt budget boundary can retain history when an eligible
internal caller deliberately reuses a run.

The manager persists the active invocation's start timestamp. After an abrupt
stop, its uncertain elapsed interval counts toward the active limit until startup.
Normal completion records monotonic elapsed time and clears that timestamp.
Startup marks unfinished work interrupted, sweeps expired owned sources, and never
automatically invokes a checkpoint. Secrets remain in the volatile environment
manager. Cleanup retains safe MongoDB evidence and reports incomplete removal.

## File ownership

- `repository/acquisition.py` owns exact source fetch and temporary clone cleanup.
  `analysis/cache.py` owns analysis reuse and refreshed scan metadata.
- `storage/mongodb.py`, the rewritten scan/artifact stores and worktree manager own
  MongoDB records. `domain/workflow_models.py` defines requests and lifecycle metadata.
- `workflows/stage4_graph.py` owns native graph steps and pauses.
  `workflows/manager.py` owns tasks, the VM slot, deadlines, cancellation and startup.
- `api/workflow_routes.py`, `main.py` and `static/index.html` expose the workflow.
  Existing artifact read and secure environment endpoints are reused.
- Artifact service/generator/models and Git publisher changes extract the existing
  operations, keep bounded attempt history, and move blocking calls to threads.
- `scripts/demo_stage2.py` and `scripts/demo_stage3.py` are deleted. Their reusable
  offline helpers live under `tests`; no alternate production executor remains.
- Dependency/settings/example changes add MongoDB and lifecycle limits. Existing
  tests, fixture constructors and usage docs now follow the current storage and APIs.

New classes and functions include short English explanations. The focused pstack
review uses test reproductions for confirmed defects. No independent review agents,
new architecture, deployment, or project push were used.
