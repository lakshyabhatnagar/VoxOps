from pathlib import Path

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.security import redact
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, RepairIntent, TaskResult
from voxops.domain.execution_models import ExecutionDiagnostic, ExecutionPlan
from voxops.execution.diagnostics import repair_target
from voxops.storage.artifact_store import digest


async def propose_repair(
    run: ArtifactRun,
    root: Path,
    plan: ExecutionPlan,
    diagnostic: ExecutionDiagnostic,
    generator: ArtifactGenerator,
    budget: ModelBudget,
) -> TaskResult | None:
    if plan.artifact_plan_id != run.plan.plan_id or plan.artifact_plan_digest != digest(
        run.plan.model_dump(mode="json")
    ):
        raise AppError("proposal_conflict", "Repair plan differs from the artifact plan.", 409)
    target = repair_target(diagnostic, plan, run.plan, root)
    if target is None:
        return None
    task = run.plan.tasks[target.task_id]
    previous = run.tasks.get(task.task_id)
    applied = run.applied_hashes.get(task.task_id)
    if previous is None or previous.proposal is None or applied is None:
        raise AppError("proposal_conflict", "Repair lacks applied proposal authority.", 409)
    proposal = previous.proposal
    if (
        proposal.task_id != task.task_id
        or proposal.target_path != task.target_path
        or proposal.artifact_type != task.artifact_type
        or proposal.original_hash != task.existing_hash
        or (proposal.content_hash != applied and proposal.parent_hash != applied)
    ):
        raise AppError("proposal_conflict", "Repair proposal ownership changed.", 409)
    return await generator.generate(
        run.run_id,
        root,
        task,
        run.plan,
        budget,
        repair=RepairIntent(
            expected_applied_hash=applied,
            feedback=redact(
                "\n".join([target.feedback, diagnostic.message, *diagnostic.evidence])
            )[:2000],
        ),
    )
