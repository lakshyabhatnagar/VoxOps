import asyncio
from pathlib import Path
from threading import Event
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite, RetryPolicy, Send, interrupt
from pymongo.errors import AutoReconnect, NetworkTimeout

from voxops.analysis.cache import AnalysisCache
from voxops.core.errors import AppError
from voxops.domain.workflow_models import WorkflowRecord


# Merge distinct unit receipts while refusing competing results for the same verification wave.
# Repairs start a new wave using LangGraph's explicit overwrite operation.
def merge_unit_outcomes(left: dict, right: dict) -> dict:
    combined = dict(left)
    for unit_id, outcome in right.items():
        if unit_id in combined and combined[unit_id] != outcome:
            raise AppError(
                "unit_result_conflict", "Unit returned conflicting verification evidence.", 409
            )
        combined[unit_id] = outcome
    return combined


# Persist only workflow/domain references and short per-unit outcomes in checkpoints.
# Source files, full runs, model prompts and environment values never belong in graph state.
class VoxOpsState(TypedDict, total=False):
    workflow_id: str
    artifact_run_id: str
    repository_source_id: str
    unit_outcomes: Annotated[dict, merge_unit_outcomes]
    unit_id: str


# Coordinate the existing stage algorithms through native checkpoint and human-input boundaries.
# The runner remains the sole dependency scheduler and Docker implementation.
class Stage4Workflow:
    # Bind specialized steps to one graph and the maintained MongoDB saver supplied by lifespan.
    # Tests may inject a memory saver while retaining the actual graph and domain services.
    def __init__(self, service, acquirer, checkpointer):
        self.service, self.acquirer = service, acquirer
        self.database = service.store.database
        self.cache = AnalysisCache(service.analysis)
        builder = StateGraph(VoxOpsState)
        for name in [
            "acquire",
            "scan",
            "plan",
            "source_review",
            "generate",
            "environment",
            "verify",
            "unit_receipt",
            "collect",
            "repair",
            "final_review",
            "publish",
        ]:
            options = (
                {
                    "retry_policy": RetryPolicy(
                        max_attempts=2, retry_on=(AutoReconnect, NetworkTimeout)
                    )
                }
                if name == "scan"
                else {}
            )
            builder.add_node(name, getattr(self, name), **options)
        builder.add_edge(START, "acquire")
        for before, after in [
            ("acquire", "scan"),
            ("scan", "plan"),
            ("plan", "source_review"),
            ("source_review", "generate"),
            ("generate", "environment"),
            ("environment", "verify"),
            ("unit_receipt", "collect"),
            ("repair", "environment"),
            ("final_review", "publish"),
            ("publish", END),
        ]:
            builder.add_edge(before, after)
        builder.add_conditional_edges("verify", self.dispatch_receipts)
        builder.add_conditional_edges("collect", self.route_outcome)
        self.graph = builder.compile(checkpointer=checkpointer)

    # Read the durable request and workspace links for a node without copying them into state.
    # A missing workflow cannot begin acquisition, resume, or publication.
    async def record(self, workflow_id) -> WorkflowRecord:
        raw = await asyncio.to_thread(self.database.workflows.find_one, {"_id": workflow_id})
        if raw is None:
            raise AppError("workflow_not_found", "Unknown workflow.", 404)
        raw.pop("_id")
        return WorkflowRecord.model_validate(raw)

    # Save only newly established references, preserving lifecycle updates from the task manager.
    # Graph nodes do not maintain their own parallel current-step or completed-step records.
    async def reference(self, workflow_id, **fields):
        await asyncio.to_thread(
            self.database.workflows.update_one, {"_id": workflow_id}, {"$set": fields}
        )

    # Acquire an immutable GitHub revision, or validate a previously acquired temporary checkout.
    # Persist its reference before the next graph step so explicit replay can reuse it safely.
    async def acquire(self, state):
        record = await self.record(state["workflow_id"])
        if record.repository_source_id:
            source = await asyncio.to_thread(self.acquirer.load, record.repository_source_id)
        else:
            cancel = Event()
            acquisition = asyncio.create_task(
                asyncio.to_thread(
                    self.acquirer.acquire,
                    record.request.repository_url,
                    record.request.ref,
                    cancel=cancel,
                )
            )
            try:
                source = await asyncio.shield(acquisition)
                await self.reference(record.workflow_id, repository_source_id=source.id)
            except asyncio.CancelledError:
                cancel.set()
                try:
                    source = await acquisition
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(self.acquirer.remove, source.id)
                raise
        return {"repository_source_id": source.id}

    # Perform exact-revision/content cache lookup before invoking existing Stage 1 analysis.
    # The saved analysis always refers to this workflow's current temporary checkout.
    async def scan(self, state):
        record = await self.record(state["workflow_id"])
        source = await asyncio.to_thread(self.acquirer.load, state["repository_source_id"])
        result = await self.cache.analyze(
            Path(source.path), source.repository, record.request.use_llm
        )
        await self.reference(record.workflow_id, scan_id=result.scan.id)
        return {}

    # Create the existing artifact plan/run using cached analysis and validated strategy choices.
    # Reused artifact runs retain their accepted tasks while receiving a new user-attempt budget.
    async def plan(self, state):
        record = await self.record(state["workflow_id"])
        if record.artifact_run_id:
            run = await asyncio.to_thread(self.service.store.load_run, record.artifact_run_id)
        else:
            source = await asyncio.to_thread(self.acquirer.load, state["repository_source_id"])
            analysis = await asyncio.to_thread(self.service.scans.load, record.scan_id)
            request = record.request
            strategies = {
                unit.id: request.strategies.get(
                    unit.id, request.strategies.get(unit.root_path, "container")
                )
                for unit in analysis.detected_units
            }
            plan = await self.service.plan(
                source.path, strategies, request.provider, request.patches, analysis=analysis
            )
            owner, name = source.url.removesuffix(".git").rsplit("/", 2)[-2:]
            if request.owner and (request.owner, request.name) != (owner, name):
                await asyncio.to_thread(
                    self.acquirer.executor.run, Path(source.path), "remote", "remove", "origin"
                )
            run = await self.service.start(
                plan.plan_id, request.owner or owner, request.name or name, request.selected_paths
            )
            await self.reference(record.workflow_id, artifact_run_id=run.run_id)
        await asyncio.to_thread(self.service.begin_workflow_attempt, run.run_id, record.workflow_id)
        return {"artifact_run_id": run.run_id}

    # Pause for baseline/source approval only when the existing publisher requires it.
    # Approval carries the exact review identity; publication remains in later service operations.
    async def source_review(self, state):
        run = await asyncio.to_thread(self.service.store.load_run, state["artifact_run_id"])
        if run.status == "awaiting_source_publication":
            review = max(run.reviews.values(), key=lambda item: item.created_at)
            decision = interrupt(
                {
                    "kind": "source_review",
                    "artifact_run_id": run.run_id,
                    "review_id": review.review_id,
                    "digest": review.digest,
                }
            )
            await self.approve(run.run_id, review, decision)
        return {}

    # Generate/apply only missing or invalid planned artifacts and prepare the environment form.
    # Docker work starts in the following verification node rather than inside generation.
    async def generate(self, state):
        run = await self.service.generate_candidate(state["artifact_run_id"])
        if run.execution is None:
            raise AppError("artifacts_incomplete", "Candidate generation did not complete.", 409)
        return {}

    # Pause with safe form metadata when the existing volatile session lacks required values.
    # Submitted values are read from that session, never from a resume payload or checkpoint.
    async def environment(self, state):
        run_id = state["artifact_run_id"]
        while True:
            form = await asyncio.to_thread(self.service.environment_form, run_id)
            try:
                await asyncio.to_thread(self.service.environment_authority, run_id)
                return {}
            except AppError as error:
                if error.category not in {"env_unresolved", "env_session_lost"}:
                    raise
            interrupt(
                {
                    "kind": "environment",
                    "artifact_run_id": run_id,
                    "schema_digest": form["schema_digest"],
                    "names": [
                        item.name
                        for item in form["schema"].requirements
                        if item.resolution in {"missing", "ambiguous"}
                    ],
                }
            )

    # Run independent units concurrently through the existing dependency-aware Docker runner.
    # It saves attempts/resources incrementally and reuses successful matching unit inputs.
    async def verify(self, state):
        await self.service.verify_candidate(state["artifact_run_id"])
        return {}

    # Dispatch small independent receipts after the runner has saved coherent unit evidence.
    # This avoids creating a second Docker scheduler that bypasses its prerequisites or cleanup.
    def dispatch_receipts(self, state):
        run = self.service.store.load_run(state["artifact_run_id"])
        return [
            Send("unit_receipt", {**state, "unit_id": unit_id})
            for unit_id in sorted(run.execution.units)
        ] or "collect"

    # Read one unit's canonical verification evidence and emit only its status/input reference.
    # Parallel graph writes merge through the conflict-checking reducer rather than replacing runs.
    async def unit_receipt(self, state):
        run = await asyncio.to_thread(self.service.store.load_run, state["artifact_run_id"])
        unit = run.execution.units[state["unit_id"]]
        return {
            "unit_outcomes": {
                unit.unit_id: {"status": unit.status, "input_digest": unit.input_digest}
            }
        }

    # Require a complete receipt set matching the canonical unit evidence before routing.
    # A missing or conflicting outcome cannot authorize a final review.
    async def collect(self, state):
        run = await asyncio.to_thread(self.service.store.load_run, state["artifact_run_id"])
        expected = {
            key: {"status": value.status, "input_digest": value.input_digest}
            for key, value in run.execution.units.items()
        }
        if not expected or expected != state.get("unit_outcomes"):
            raise AppError(
                "verification_incomplete", "Unit verification evidence is incomplete.", 409
            )
        return {}

    # Permit final approval only when every required unit passed its local checks.
    # External blockers and incomplete verification stop this workflow without a waiver path.
    def route_outcome(self, state):
        statuses = {item["status"] for item in state["unit_outcomes"].values()}
        if statuses == {"passed"}:
            return "final_review"
        if "blocked" in statuses or "verification_incomplete" in statuses:
            raise AppError(
                "verification_blocked",
                "Required verification is blocked; inspect unit diagnostics.",
                409,
            )
        return "repair"

    # Repair the first failed unit in deterministic order using its persisted cycle allowance.
    # A changed candidate starts a fresh receipt wave and rechecks affected inputs in the runner.
    async def repair(self, state):
        run = await asyncio.to_thread(self.service.store.load_run, state["artifact_run_id"])
        failed = sorted(key for key, unit in run.execution.units.items() if unit.status == "failed")
        if not failed:
            raise AppError("verification_failed", "No supported failing unit can be repaired.", 409)
        before = dict(run.applied_hashes)
        repaired = await self.service.repair_candidate(run.run_id, failed[0])
        if repaired.applied_hashes == before:
            raise AppError("repair_exhausted", "No authorized repair remains for this unit.", 409)
        return {"unit_outcomes": Overwrite({})}

    # Ask the person to approve the exact checked candidate and its saved review digest.
    # Resume rechecks verification before accepting the decision through the existing approval gate.
    async def final_review(self, state):
        run = await asyncio.to_thread(self.service.store.load_run, state["artifact_run_id"])
        if run.execution.status != "passed":
            raise AppError(
                "verification_incomplete", "All required checks must pass before approval.", 409
            )
        review = max(
            (r for r in run.reviews.values() if r.purpose == "artifact_publication"),
            key=lambda item: item.created_at,
        )
        decision = interrupt(
            {
                "kind": "final_review",
                "artifact_run_id": run.run_id,
                "review_id": review.review_id,
                "digest": review.digest,
            }
        )
        await self.approve(run.run_id, review, decision)
        return {}

    # Validate the decision against exactly the review shown before the native interrupt.
    # Rejection is terminal and cannot fall through to publication.
    async def approve(self, run_id, review, decision):
        if (
            not isinstance(decision, dict)
            or decision.get("review_id") != review.review_id
            or decision.get("digest") != review.digest
            or decision.get("decision") not in {"approve", "reject"}
        ):
            raise AppError("stale_review", "Resume must identify the exact pending review.", 409)
        await asyncio.to_thread(
            self.service.approve, run_id, review.review_id, review.digest, decision["decision"]
        )
        if decision["decision"] == "reject":
            raise AppError("approval_rejected", "The proposed changes were rejected.", 409)

    # Publish only after the final interrupt through the existing Git and environment guards.
    # The publisher verifies remote SHA and returns a branch handoff without cloud deployment.
    async def publish(self, state):
        run = await self.service.publish(state["artifact_run_id"])
        if run.result is None or run.result.commit_sha != run.result.remote_sha:
            raise AppError("publication_unverified", "Remote branch SHA was not verified.", 409)
        return {}
