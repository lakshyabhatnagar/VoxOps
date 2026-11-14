import asyncio
import time
from datetime import UTC, datetime, timedelta

from langgraph.types import Command
from pymongo.errors import PyMongoError

from voxops.artifacts.security import check_secret
from voxops.core.errors import AppError
from voxops.domain.workflow_models import WorkflowRecord

TERMINAL = {"failed", "cancelled", "expired", "ready_for_deployment", "rejected"}


# Own background workflow tasks and the single active execution slot in one FastAPI process.
# MongoDB retains lookup/deadline metadata while LangGraph owns resumable workflow position.
class WorkflowManager:
    # Share the graph and domain services, and allocate only process-local synchronization.
    # Waiting for human input releases the execution slot without discarding its checkpoint.
    def __init__(self, workflow):
        self.workflow, self.graph = workflow, workflow.graph
        self.database, self.service = workflow.database, workflow.service
        self.settings, self.acquirer = self.service.settings, workflow.acquirer
        self.tasks = {}
        self.slot = asyncio.Lock()
        self.registration = asyncio.Lock()
        self.sweeper = None

    # Atomically update selected metadata rather than replacing a concurrent lifecycle change.
    # Timestamps use JSON-safe UTC values consistently with the existing domain records.
    async def update(self, workflow_id, **fields):
        fields["updated_at"] = datetime.now(UTC).isoformat()
        await asyncio.to_thread(
            self.database.workflows.update_one, {"_id": workflow_id}, {"$set": fields}
        )

    # Initialize the idempotency index and mark interrupted processes without auto-resuming them.
    # A lightweight cleanup task enforces human-input and absolute workspace deadlines.
    async def start(self):
        await asyncio.to_thread(
            self.database.workflows.create_index, "idempotency_key", unique=True
        )
        await asyncio.to_thread(
            self.database.analysis_cache.create_index,
            [("repository", 1), ("content", 1), ("policy", 1)],
        )
        await asyncio.to_thread(self.database.artifact_tasks.create_index, "run_id")
        interrupted = await asyncio.to_thread(
            lambda: list(self.database.workflows.find({"status": {"$in": ["running", "created"]}}))
        )
        for raw in interrupted:
            started = raw.get("active_started_at")
            elapsed = (
                max(0, (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds())
                if started
                else 0
            )
            await self.update(
                raw["_id"],
                status="interrupted",
                active_started_at=None,
                active_seconds=raw.get("active_seconds", 0) + elapsed,
            )
        await self.sweep()
        self.sweeper = asyncio.create_task(self.cleanup_loop())

    # Reserve the VM immediately or return busy without building an implicit request queue.
    # Registration serializes duplicate creation and concurrent resumes before acquiring it.
    async def reserve(self):
        if self.slot.locked():
            raise AppError("server_busy", "Another workflow is using this VM; retry later.", 409)
        await self.slot.acquire()

    # Persist one validated request and launch its managed task before returning a workflow ID.
    # Repeating the same client key returns the same record without starting another operation.
    async def create(self, request, key, *, artifact_run_id=None, repository_source_id=None):
        check_secret("workflow-request.json", request.model_dump_json().encode())
        async with self.registration:
            existing = await asyncio.to_thread(
                self.database.workflows.find_one, {"idempotency_key": key}
            )
            if existing:
                existing.pop("_id")
                record = WorkflowRecord.model_validate(existing)
                if record.request != request:
                    raise AppError(
                        "idempotency_conflict", "This key belongs to a different request.", 409
                    )
                return record
            record = WorkflowRecord(
                idempotency_key=key,
                request=request,
                artifact_run_id=artifact_run_id,
                repository_source_id=repository_source_id,
            )
            await self.reserve()
            try:
                await asyncio.to_thread(
                    self.database.workflows.insert_one,
                    {"_id": record.workflow_id, **record.model_dump(mode="json")},
                )
            except BaseException:
                self.slot.release()
                raise
            self.tasks[record.workflow_id] = asyncio.create_task(
                self.drive(record.workflow_id, {"workflow_id": record.workflow_id})
            )
            return record

    # Execute only while the slot is owned, then persist safe waiting or terminal metadata.
    # Active time accumulates across resumes while each invocation has a bounded remaining budget.
    async def drive(self, workflow_id, command):
        started = time.monotonic()
        try:
            record = await self.workflow.record(workflow_id)
            await self.update(
                workflow_id,
                status="running",
                waiting_since=None,
                error_category=None,
                active_started_at=datetime.now(UTC).isoformat(),
            )
            remaining = self.settings.active_execution_timeout_minutes * 60 - record.active_seconds
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                result = await self.graph.ainvoke(
                    command,
                    {
                        "configurable": {"thread_id": workflow_id},
                        "recursion_limit": 500,
                    },
                )
            pending = result.get("__interrupt__", ())
            if pending:
                kind = pending[0].value["kind"]
                await self.update(
                    workflow_id,
                    status=("waiting_for_env" if kind == "environment" else "waiting_for_approval"),
                    waiting_since=datetime.now(UTC).isoformat(),
                )
            else:
                record = await self.workflow.record(workflow_id)
                run = await asyncio.to_thread(self.service.store.load_run, record.artifact_run_id)
                if run.status != "ready_for_deployment" or run.result is None:
                    raise AppError(
                        "workflow_incomplete", "Workflow ended before verified publication.", 409
                    )
                await self.update(workflow_id, status="ready_for_deployment")
        except asyncio.CancelledError:
            record = await self.workflow.record(workflow_id)
            if record.status not in {"expired", "interrupted"}:
                await self.update(workflow_id, status="cancelled")
        except TimeoutError:
            await self.update(
                workflow_id, status="expired", error_category="active_execution_timeout"
            )
        except AppError as error:
            await self.update(
                workflow_id,
                status=("rejected" if error.category == "approval_rejected" else "failed"),
                error_category=error.category,
            )
        except Exception:
            await self.update(workflow_id, status="failed", error_category="workflow_failed")
        finally:
            try:
                await asyncio.to_thread(
                    self.database.workflows.update_one,
                    {"_id": workflow_id},
                    {
                        "$inc": {"active_seconds": time.monotonic() - started},
                        "$set": {"active_started_at": None},
                    },
                )
                record = await self.workflow.record(workflow_id)
                if record.status in TERMINAL:
                    await asyncio.shield(self.cleanup(workflow_id))
            finally:
                self.tasks.pop(workflow_id, None)
                self.slot.release()

    # Validate the exact pending form/review before resuming its existing checkpoint thread.
    # Secret values must already have been accepted through the environment submission endpoint.
    async def resume(self, workflow_id, payload):
        async with self.registration:
            record = await self.workflow.record(workflow_id)
            if record.status in TERMINAL:
                raise AppError(
                    "workflow_closed", "This workflow is closed; start a new attempt.", 409
                )
            if workflow_id in self.tasks:
                raise AppError("workflow_busy", "This workflow is already running.", 409)
            await asyncio.to_thread(self.acquirer.load, record.repository_source_id)
            config = {"configurable": {"thread_id": workflow_id}}
            state = await self.graph.aget_state(config)
            pending = state.interrupts
            if pending:
                expected = pending[0].value
                if expected["kind"] == "environment":
                    if (
                        set(payload) != {"schema_digest"}
                        or payload["schema_digest"] != expected["schema_digest"]
                    ):
                        raise AppError(
                            "env_form_stale", "Resume must identify the pending form.", 409
                        )
                    await asyncio.to_thread(
                        self.service.environment_authority, record.artifact_run_id
                    )
                elif (
                    set(payload) != {"review_id", "digest", "decision"}
                    or payload.get("review_id") != expected["review_id"]
                    or payload.get("digest") != expected["digest"]
                    or payload.get("decision") not in {"approve", "reject"}
                ):
                    raise AppError("stale_review", "Resume must identify the pending review.", 409)
                command = Command(resume=payload)
            elif record.status == "interrupted" and state.next and not payload:
                command = None
            else:
                raise AppError("workflow_not_resumable", "No eligible checkpoint is waiting.", 409)
            await self.reserve()
            self.tasks[workflow_id] = asyncio.create_task(self.drive(workflow_id, command))
            return record

    # Request cancellation through the runner, then await the managed task's cleanup.
    # Repeating cancellation for a finished workflow returns its existing status.
    async def cancel(self, workflow_id, *, expired=False, reason="workspace_expired"):
        record = await self.workflow.record(workflow_id)
        if record.status in TERMINAL:
            return record
        await self.update(
            workflow_id,
            status="expired" if expired else "cancelled",
            error_category=reason if expired else None,
        )
        if record.artifact_run_id:
            await self.service.cancel_execution(record.artifact_run_id)
        task = self.tasks.get(workflow_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self.cleanup(workflow_id)
        return await self.workflow.record(workflow_id)

    # Remove owned Docker leftovers, candidate worktrees and source clones after a terminal result.
    # Keep all MongoDB evidence and report a cleanup failure instead of claiming resources vanished.
    async def cleanup(self, workflow_id):
        record = await self.workflow.record(workflow_id)
        try:
            if record.artifact_run_id:
                run = await asyncio.to_thread(self.service.store.load_run, record.artifact_run_id)
                if run.execution and any(r.state != "removed" for r in run.execution.resources):
                    await self.service._recover(run)
                    run = await asyncio.to_thread(
                        self.service.store.load_run, record.artifact_run_id
                    )
                    if any(r.state != "removed" for r in run.execution.resources):
                        raise AppError("cleanup_failed", "Docker cleanup is incomplete.", 409)
                if run.worktree_id:
                    await asyncio.to_thread(
                        self.service.worktrees.remove, run.worktree_id, discard_changes=True
                    )
                self.service.environment.clear(run.run_id)
            if record.repository_source_id:
                await asyncio.to_thread(self.acquirer.remove, record.repository_source_id)
            await self.update(workflow_id, cleanup_complete=True, cleanup_error=None)
        except Exception:
            await self.update(workflow_id, cleanup_complete=False, cleanup_error="cleanup_failed")

    # Expire inactive human prompts and absolute workspace deadlines without resuming work.
    # Startup uses this same sweep so a stopped server does not leave expired disk contents forever.
    async def sweep(self):
        now = datetime.now(UTC)
        records = await asyncio.to_thread(
            lambda: list(self.database.workflows.find({"status": {"$nin": list(TERMINAL)}}))
        )
        for raw in records:
            raw.pop("_id")
            record = WorkflowRecord.model_validate(raw)
            deadline = record.created_at + timedelta(
                minutes=self.settings.workspace_max_lifetime_minutes
            )
            if record.repository_source_id:
                source = await asyncio.to_thread(
                    self.database.sources.find_one, {"_id": record.repository_source_id}
                )
                if source and source.get("expires_at"):
                    deadline = datetime.fromisoformat(source["expires_at"])
            idle = record.waiting_since and now >= record.waiting_since + timedelta(
                minutes=self.settings.hitl_idle_timeout_minutes
            )
            exhausted = record.active_seconds >= self.settings.active_execution_timeout_minutes * 60
            if now >= deadline or idle or exhausted:
                reason = (
                    "workspace_expired"
                    if now >= deadline
                    else ("hitl_idle_timeout" if idle else "active_execution_timeout")
                )
                await self.cancel(record.workflow_id, expired=True, reason=reason)
        sources = await asyncio.to_thread(
            lambda: list(self.database.sources.find({"status": {"$in": ["active", "acquiring"]}}))
        )
        for source in sources:
            deadline = (
                datetime.fromisoformat(source["expires_at"])
                if source.get("expires_at")
                else datetime.fromisoformat(source["created_at"])
                + timedelta(minutes=self.settings.workspace_max_lifetime_minutes)
            )
            if now >= deadline:
                await asyncio.to_thread(self.acquirer.remove, source["_id"])

    # Poll only for expiry cleanup; this task never schedules or recovers an application workflow.
    # Five-second checks keep the single-VM lifecycle simple and bounded.
    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.sweep()
            except PyMongoError:
                continue

    # Stop the expiry task and cooperatively cancel active work when the application shuts down.
    # Preserve interrupted metadata and checkpoints for eligible explicit operator resume.
    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for workflow_id, task in list(self.tasks.items()):
            await self.update(workflow_id, status="interrupted")
            record = await self.workflow.record(workflow_id)
            if record.artifact_run_id:
                await self.service.cancel_execution(record.artifact_run_id)
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
