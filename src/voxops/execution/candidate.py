"""Exact public input identity and the final publisher's fail-closed boundary."""

from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactRun, ChangeReview
from voxops.domain.execution_models import (
    CHECK_OPERATIONS,
    LEVEL_CHECKS,
    CandidateIdentity,
    EnvFormSchema,
    ExecutionAttestation,
    ExecutionPlan,
    ExecutionRun,
    IncompleteAcknowledgement,
)
from voxops.storage.artifact_store import digest


def execution_policy_fingerprint(settings: Settings) -> str:
    root = Path(__file__).resolve().parents[1]
    # Include new adapters automatically: changing interpretation invalidates old evidence.
    files = sorted(
        {
            *root.joinpath("execution").rglob("*.py"),
            *root.joinpath("workflows").glob("stage3*.py"),
            root / "domain/execution_models.py",
            root / "domain/models.py",
            root / "core/settings.py",
        }
    )
    return digest(
        {
            "code": {
                path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in files
            },
            "settings": {
                name: getattr(settings, name)
                for name in type(settings).model_fields
                if name.startswith("execution_")
                or name in {"build_timeout_seconds", "startup_timeout_seconds", "max_file_bytes"}
            },
        }
    )


def candidate_identity(
    run: ArtifactRun,
    inventory: Mapping[str, Mapping[str, str]],
    plan: ExecutionPlan,
    env_schema: EnvFormSchema,
    settings: Settings,
    execution_id: str,
) -> CandidateIdentity:
    """Inventory is the complete eligible candidate tree, never raw env-file content."""
    unit_ids = {unit.id for unit in run.plan.units}
    if set(plan.units) != unit_ids or set(run.plan.strategy_by_unit) != unit_ids:
        raise AppError("verification_stale", "Execution plan must cover every candidate unit.", 409)
    if any(plan.units[key].strategy != run.plan.strategy_by_unit[key] for key in unit_ids):
        raise AppError("verification_stale", "Execution strategy differs from artifact plan.", 409)
    artifact_plan_digest = digest(run.plan.model_dump(mode="json"))
    if (
        plan.artifact_plan_id != run.plan.plan_id
        or plan.artifact_plan_digest != artifact_plan_digest
    ):
        raise AppError(
            "verification_stale", "Execution plan is bound to a different artifact plan.", 409
        )
    if any(
        consumer.unit_id not in unit_ids
        for item in env_schema.requirements
        for consumer in item.consumers
    ):
        raise AppError("verification_stale", "Environment schema refers to an unknown unit.", 409)
    payload = {
        "artifact_run_id": run.run_id,
        "execution_id": execution_id,
        "artifact_plan_digest": artifact_plan_digest,
        "execution_plan_digest": digest(plan.model_dump(mode="json")),
        "source_digest": digest(
            {
                "repo": run.repo_fingerprint,
                "source": run.source_fingerprint,
                "scan": run.source_input_fingerprint,
                "head": run.source_head_sha,
                "branch": run.source_branch,
                "baseline": run.progress.baseline_sha,
            }
        ),
        "inventory_digest": digest(
            [
                {"path": path, "hash": facts["hash"], "mode": facts["mode"]}
                for path, facts in sorted(inventory.items())
            ]
        ),
        "env_schema_digest": digest(
            env_schema.model_dump(
                mode="json", exclude={"requirements": {"__all__": {"resolution"}}}
            )
        ),
        "policy_digest": execution_policy_fingerprint(settings),
    }
    return CandidateIdentity(**payload, digest=digest(payload))


def attestation_digest(attestation: ExecutionAttestation) -> str:
    return digest(attestation.model_dump(mode="json", exclude={"digest"}))


def unit_input_digest(plan: ExecutionPlan, unit_id: str) -> str:
    task_ids = {key for key, task in plan.tasks.items() if task.unit_id == unit_id}
    pending = list(task_ids)
    while pending:
        for dependency in plan.tasks[pending.pop()].depends_on:
            if dependency not in task_ids:
                task_ids.add(dependency)
                pending.append(dependency)
    return digest(
        {
            "unit": plan.units[unit_id].model_dump(mode="json"),
            "tasks": {key: plan.tasks[key].model_dump(mode="json") for key in sorted(task_ids)},
        }
    )


def missing_check_ids(attestation: ExecutionAttestation) -> list[str]:
    return sorted(
        f"{unit.unit_id}:{kind}"
        for unit in attestation.units.values()
        for kind in LEVEL_CHECKS[unit.required_level]
        if kind not in unit.checks or unit.checks[kind].status != "passed"
    )


def validate_acknowledgement(
    review: ChangeReview,
    acknowledgement: IncompleteAcknowledgement | None,
) -> None:
    attestation = review.execution_attestation
    if attestation is None or attestation.status != "verification_incomplete":
        if acknowledgement is not None:
            raise AppError(
                "verification_acknowledgement", "No incomplete checks to acknowledge.", 409
            )
        return
    if acknowledgement is None or (
        acknowledgement.review_id != review.review_id
        or acknowledgement.review_digest != review.digest
        or acknowledgement.attestation_digest != attestation_digest(attestation)
        or sorted(acknowledgement.missing_check_ids) != missing_check_ids(attestation)
    ):
        raise AppError(
            "verification_acknowledgement",
            "Incomplete verification requires acknowledgement of this review's missing checks.",
            409,
        )


def require_publishable(
    run: ArtifactRun,
    review: ChangeReview,
    inventory: Mapping[str, Mapping[str, str]],
    settings: Settings,
) -> ExecutionAttestation:
    """Call under the artifact project lock, after tree checks and immediately before push.

    The coordinator must invalidate evidence when ephemeral configuration changes or
    secret-session authority is lost. Public identities deliberately never hash secrets.
    """
    if run.execution is None or review.execution_attestation is None:
        raise AppError("verification_required", "Current execution verification is required.", 409)
    try:
        # Revalidate in-place mutations and unvalidated model copies.
        execution = ExecutionRun.model_validate(run.execution.model_dump(mode="json"))
        attestation = ExecutionAttestation.model_validate(
            review.execution_attestation.model_dump(mode="json")
        )
        expected = candidate_identity(
            run,
            inventory,
            execution.plan,
            execution.env_schema,
            settings,
            execution.execution_id,
        )
    except (ValueError, ValidationError) as error:
        raise AppError(
            "verification_stale", "Execution verification record is invalid.", 409
        ) from error
    current = execution.attestation
    if (
        execution.artifact_run_id != run.run_id
        or execution.status not in {"passed", "verification_incomplete"}
        or current is None
        or current.status != execution.status
        or execution.identity != expected
        or attestation.identity != expected
        or current != attestation
        or attestation.status not in {"passed", "verification_incomplete"}
        or attestation.digest != attestation_digest(attestation)
        or review.attestation_digest != attestation_digest(attestation)
        or set(attestation.units) != set(execution.plan.units)
        or review.digest != digest(review.model_dump(mode="json", exclude={"digest"}))
    ):
        raise AppError(
            "verification_stale", "Execution verification changed or is not publishable.", 409
        )
    for key, unit in attestation.units.items():
        spec = execution.plan.units[key]
        if unit.required_level != spec.required_level:
            raise AppError(
                "verification_stale", "Verified checks differ from the execution plan.", 409
            )
        if spec.strategy != "static" and spec.required_level not in {
            "http_responded",
            "application_health_confirmed",
        }:
            raise AppError("verification_stale", "Server units require HTTP verification.", 409)
        if spec.strategy == "static" and spec.required_level not in {
            "static_output_verified",
            "static_http_responded",
        }:
            raise AppError("verification_stale", "Static units require output verification.", 409)
        declared = {
            item.name
            for item in execution.env_schema.requirements
            if item.necessity == "required"
            and item.resolution in {"existing_env", "supplied", "default"}
            and any(consumer.unit_id == key for consumer in item.consumers)
        }
        if unit.input_digest != unit_input_digest(execution.plan, key):
            raise AppError("verification_stale", "Unit verification inputs changed.", 409)
        operations = {
            task.operation: task for task in execution.plan.tasks.values() if task.unit_id == key
        }
        for check in unit.checks.values():
            if check.status != "passed":
                continue
            task = operations.get(CHECK_OPERATIONS[check.kind])
            attempts = execution.attempts.get(task.task_id, []) if task else []
            current_attempts = [
                attempt for attempt in attempts if attempt.input_digest == task.input_digest
            ]
            observed = current_attempts[-1] if current_attempts else None
            if (
                observed is None
                or observed.attempt_id != check.attempt_id
                or observed.status != "passed"
                or observed.ended_at is None
                or observed.exit_code not in {None, 0}
                or not set(check.evidence) <= set(observed.evidence)
            ):
                raise AppError(
                    "verification_stale", "Passing check lacks its current execution attempt.", 409
                )
        if any(
            item.category == "EXTERNAL_SERVICE_UNAVAILABLE" and item.dependency_name not in declared
            for item in unit.diagnostics
        ):
            raise AppError(
                "verification_stale", "External blocker is not a declared dependency.", 409
            )
        for diagnostic in unit.diagnostics:
            if diagnostic.category != "EXTERNAL_SERVICE_UNAVAILABLE":
                continue
            task = execution.plan.tasks.get(diagnostic.task_id)
            attempts = execution.attempts.get(diagnostic.task_id, [])
            current_attempts = [
                attempt
                for attempt in attempts
                if task and attempt.input_digest == task.input_digest
            ]
            observed = current_attempts[-1] if current_attempts else None
            if (
                task is None
                or task.unit_id != key
                or task.operation not in {"startup", "probe"}
                or observed is None
                or observed.status not in {"failed", "blocked"}
                or observed.ended_at is None
                or diagnostic not in observed.diagnostics
            ):
                raise AppError(
                    "verification_stale", "External blocker lacks a recorded runtime failure.", 409
                )
        if (
            "health" in unit.checks
            and unit.checks["health"].status == "passed"
            and (
                spec.probe is None
                or not spec.probe.health_contract
                or unit.checks["health"].http_status not in spec.probe.success_statuses
            )
        ):
            raise AppError(
                "verification_stale", "Health evidence lacks the declared contract.", 409
            )
    if any(
        item.necessity == "unknown"
        or item.resolution == "ambiguous"
        or (item.necessity == "required" and item.resolution == "missing")
        for item in execution.env_schema.requirements
    ):
        raise AppError(
            "verification_stale", "Unresolved environment requirements block verification.", 409
        )
    approval = run.approvals.get(review.review_id)
    if approval is None or approval.decision != "approve" or approval.digest != review.digest:
        raise AppError("approval_required", "Exact final review approval is required.", 409)
    validate_acknowledgement(review, approval.incomplete_acknowledgement)
    return attestation
