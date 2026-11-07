import asyncio

import pytest
from test_stage2_git import approve, artifacts, git, scenario

from voxops.core.errors import AppError


def prepared(tmp_path):
    ctx = scenario(tmp_path)
    asyncio.run(ctx.publisher.preflight(ctx.run, []))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    location = artifacts(ctx)
    return ctx, location


def test_direct_publish_requires_execution_before_commit_or_push(tmp_path):
    ctx, location = prepared(tmp_path)
    approve(ctx.run, "artifact_publication")
    original = ctx.executor.head(location)
    with pytest.raises(AppError, match="verification"):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert ctx.executor.head(location) == original
    assert not ctx.run.progress.artifact_sha
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)") == b"refs/heads/main\n"


def test_environment_resolution_does_not_change_identity(tmp_path):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    before = ctx.run.execution.identity
    ctx.run.execution.env_schema.requirements[0].resolution = "supplied"
    assert identity(ctx, location) == before


def test_server_cannot_use_process_only_or_redirect_health():
    from pydantic import ValidationError

    from voxops.domain.execution_models import CheckResult, UnitExecutionSpec

    with pytest.raises(ValidationError):
        UnitExecutionSpec(unit_id="app", strategy="container", required_level="process_started")
    with pytest.raises(ValidationError):
        CheckResult(
            check_id="app:health",
            kind="health",
            status="passed",
            evidence=["302"],
            http_status=302,
            attempt_id="probe-attempt",
        )


def identity(ctx, location):
    from voxops.execution.candidate import candidate_identity
    from voxops.repository.snapshot import capture_source

    execution = ctx.run.execution
    return candidate_identity(
        ctx.run,
        capture_source(location)[1],
        execution.plan,
        execution.env_schema,
        ctx.publisher.settings,
        execution.execution_id,
    )


def setup_execution(
    ctx, location, incomplete=False, *, schema_only=False, frontend=False,
    dependency_required=True, dependency_resolution="existing_env",
):
    from datetime import UTC, datetime

    from voxops.domain.execution_models import (
        CHECK_OPERATIONS,
        CheckResult,
        EnvConsumer,
        EnvFormSchema,
        EnvRequirement,
        ExecutionAttempt,
        ExecutionAttestation,
        ExecutionDiagnostic,
        ExecutionPlan,
        ExecutionRun,
        ExecutionTask,
        ProbeSpec,
        UnitExecutionSpec,
        UnitVerification,
    )
    from voxops.domain.models import (
        ConfigRequirement,
        DeployableUnit,
        FrameworkDetection,
        RuntimeSpec,
    )
    from voxops.execution.candidate import (
        attestation_digest,
        candidate_identity,
        unit_input_digest,
    )
    from voxops.repository.snapshot import capture_source
    from voxops.storage.artifact_store import digest

    unit = DeployableUnit(
        id="app",
        name="app",
        root_path=".",
        role="static_frontend" if frontend else "http_service",
        framework="react_vite" if frontend else "fastapi",
        language="javascript" if frontend else "python",
        runtime=RuntimeSpec(language="javascript" if frontend else "python"),
        config_requirements=(
            [] if schema_only else [ConfigRequirement(name="DATABASE_URL", necessity="required")]
        ),
        detection=FrameworkDetection(
            framework="react_vite" if frontend else "fastapi",
            root_path=".",
            role_candidate="static_frontend" if frontend else "http_service",
            evidence=[],
            confidence=1,
        ),
    )
    ctx.run.plan.units = [unit]
    ctx.run.plan.strategy_by_unit = {"app": "container"}
    spec = UnitExecutionSpec(
        unit_id="app",
        strategy="container",
        required_level="http_responded",
        probe=ProbeSpec(port=8000),
    )
    plan = ExecutionPlan(
        plan_id="exec-plan",
        artifact_plan_id=ctx.run.plan.plan_id,
        artifact_plan_digest=digest(ctx.run.plan.model_dump(mode="json")),
        units={"app": spec},
        tasks={
            operation: ExecutionTask(
                task_id=operation,
                unit_id="app",
                operation=operation,
                input_digest="a" * 64,
                depends_on=[]
                if operation == "build"
                else ["build" if operation == "startup" else "startup"],
            )
            for operation in ("build", "startup", "probe")
        },
    )
    schema = EnvFormSchema(
        source_digest="a" * 64,
        requirements=[
            EnvRequirement(
                field_id="database",
                name="DATABASE_URL",
                consumers=[EnvConsumer(unit_id="app", phase="runtime")],
                necessity="required" if dependency_required else "optional",
                resolution=dependency_resolution,
            )
        ],
    )
    candidate = candidate_identity(
        ctx.run, capture_source(location)[1], plan, schema, ctx.publisher.settings, "execution"
    )
    checks = {
        k: CheckResult(
            check_id=f"app:{k}",
            kind=k,
            status="passed",
            evidence=["observed"],
            http_status=200 if k == "http" else None,
            attempt_id=f"{CHECK_OPERATIONS[k]}-attempt",
        )
        for k in ("build", "startup", "http")
    }
    diagnostics = []
    if incomplete:
        diagnostics = [
            ExecutionDiagnostic(
                diagnostic_id="dependency",
                unit_id="app",
                task_id="startup",
                category="EXTERNAL_SERVICE_UNAVAILABLE",
                stage="startup",
                message="unavailable",
                dependency_name="DATABASE_URL",
                external_signal="connection",
                evidence=["connection refused"],
            )
        ]
        for k in ("startup", "http"):
            checks[k] = CheckResult(
                check_id=f"app:{k}", kind=k, status="blocked", diagnostic_ids=["dependency"]
            )
    status = "verification_incomplete" if incomplete else "passed"
    verified = UnitVerification(
        unit_id="app",
        input_digest=unit_input_digest(plan, "app"),
        required_level="http_responded",
        achieved_level="build_passed" if incomplete else "http_responded",
        status=status,
        checks=checks,
        diagnostics=diagnostics,
    )
    attestation = ExecutionAttestation(identity=candidate, status=status, units={"app": verified})
    attestation.digest = attestation_digest(attestation)
    ctx.run.execution = ExecutionRun(
        execution_id="execution",
        artifact_run_id=ctx.run.run_id,
        epoch="epoch",
        status=status,
        plan=plan,
        env_schema=schema,
        identity=candidate,
        attestation=attestation,
        attempts={
            operation: [
                ExecutionAttempt(
                    attempt_id=f"{operation}-attempt",
                    task_id=operation,
                    unit_id="app",
                    input_digest="a" * 64,
                    attempt_number=1,
                    ended_at=datetime.now(UTC),
                    status="blocked" if incomplete and operation != "build" else "passed",
                    exit_code=1 if incomplete and operation != "build" else 0,
                    evidence=["observed"],
                    diagnostics=diagnostics if operation == "startup" else [],
                )
            ]
            for operation in ("build", "startup", "probe")
        },
    )
    return ctx.publisher.artifact_review(ctx.run)


def test_passed_verification_publishes_exact_commit(tmp_path):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    approve(ctx.run, "artifact_publication")
    receipt = asyncio.run(ctx.publisher.publish(ctx.run))
    assert receipt.verification_status == "passed"
    assert git(ctx.bare, "rev-parse", ctx.run.branch).decode().strip() == receipt.commit_sha


def test_http_plan_requires_startup_and_probe_tasks(tmp_path):
    from pydantic import ValidationError

    from voxops.domain.execution_models import ExecutionPlan

    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    data = ctx.run.execution.plan.model_dump()
    data["tasks"] = {
        key: task for key, task in data["tasks"].items() if task["operation"] == "build"
    }
    with pytest.raises(ValidationError, match="verification operations"):
        ExecutionPlan.model_validate(data)


def test_passing_checks_need_recorded_current_operation_attempts(tmp_path):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    ctx.run.execution.attempts.clear()
    approve(ctx.run, "artifact_publication")
    with pytest.raises(AppError, match="execution attempt"):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""


def test_container_frontend_can_verify_its_http_server(tmp_path):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location, frontend=True)
    approve(ctx.run, "artifact_publication")
    receipt = asyncio.run(ctx.publisher.publish(ctx.run))
    assert receipt.verification_status == "passed"


def test_stage3_declared_dependency_can_be_acknowledged(tmp_path):
    from voxops.artifacts.review import approve_review
    from voxops.domain.execution_models import IncompleteAcknowledgement
    from voxops.execution.candidate import missing_check_ids

    ctx, location = prepared(tmp_path)
    review = setup_execution(ctx, location, incomplete=True, schema_only=True)
    acknowledgement = IncompleteAcknowledgement(
        review_id=review.review_id,
        review_digest=review.digest,
        attestation_digest=review.attestation_digest,
        missing_check_ids=missing_check_ids(review.execution_attestation),
        acknowledged=True,
    )
    approve_review(ctx.run, review.review_id, review.digest, "approve", acknowledgement)
    receipt = asyncio.run(ctx.publisher.publish(ctx.run))
    assert receipt.verification_status == "verification_incomplete"


def test_unconfigured_optional_key_cannot_justify_incomplete_publication(tmp_path):
    from voxops.artifacts.review import approve_review
    from voxops.domain.execution_models import IncompleteAcknowledgement
    from voxops.execution.candidate import missing_check_ids

    ctx, location = prepared(tmp_path)
    review = setup_execution(
        ctx, location, incomplete=True, dependency_required=False, dependency_resolution="missing"
    )
    acknowledgement = IncompleteAcknowledgement(
        review_id=review.review_id, review_digest=review.digest,
        attestation_digest=review.attestation_digest,
        missing_check_ids=missing_check_ids(review.execution_attestation), acknowledged=True,
    )
    approve_review(ctx.run, review.review_id, review.digest, "approve", acknowledgement)
    with pytest.raises(AppError, match="declared dependency"):
        asyncio.run(ctx.publisher.publish(ctx.run))


def test_incomplete_requires_current_exact_acknowledgement(tmp_path):
    from voxops.artifacts.review import approve_review
    from voxops.domain.execution_models import IncompleteAcknowledgement
    from voxops.execution.candidate import missing_check_ids

    ctx, location = prepared(tmp_path)
    review = setup_execution(ctx, location, incomplete=True)
    with pytest.raises(AppError, match="acknowledgement"):
        approve_review(ctx.run, review.review_id, review.digest, "approve")
    acknowledgement = IncompleteAcknowledgement(
        review_id=review.review_id,
        review_digest=review.digest,
        attestation_digest=review.attestation_digest,
        missing_check_ids=["app:http"],
        acknowledged=True,
    )
    with pytest.raises(AppError, match="acknowledgement"):
        approve_review(ctx.run, review.review_id, review.digest, "approve", acknowledgement)
    acknowledgement.missing_check_ids = missing_check_ids(review.execution_attestation)
    approve_review(ctx.run, review.review_id, review.digest, "approve", acknowledgement)
    receipt = asyncio.run(ctx.publisher.publish(ctx.run))
    assert receipt.verification_status == "verification_incomplete"
    assert git(ctx.bare, "rev-parse", ctx.run.branch).decode().strip() == receipt.commit_sha


@pytest.mark.parametrize(
    "change", ["file", "mode", "source", "strategy", "schema", "policy", "evidence"]
)
def test_changed_candidate_cannot_publish(tmp_path, change):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    approve(ctx.run, "artifact_publication")
    if change == "file":
        (location / ".dockerignore").write_text("changed\n")
    elif change == "mode":
        (location / ".dockerignore").chmod(0o755)
    elif change == "source":
        (ctx.source / "app.py").write_text("changed\n")
    elif change == "strategy":
        ctx.run.execution.plan.units["app"].strategy = "native"
    elif change == "schema":
        ctx.run.execution.env_schema.revision += 1
    elif change == "policy":
        ctx.publisher.settings.execution_max_parallel = 3
    elif change == "evidence":
        ctx.run.execution.attestation.units["app"].checks["http"].evidence.append("changed")
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""


def test_incomplete_cannot_hide_a_failed_check(tmp_path):
    from pydantic import ValidationError

    from voxops.domain.execution_models import UnitVerification

    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location, incomplete=True)
    data = ctx.run.execution.attestation.units["app"].model_dump()
    data["checks"]["startup"]["status"] = "failed"
    with pytest.raises(ValidationError):
        UnitVerification.model_validate(data)


@pytest.mark.parametrize("change", ["missing_attempt", "unattached_diagnostic"])
def test_incomplete_requires_recorded_external_failure(tmp_path, change):
    from voxops.artifacts.review import approve_review
    from voxops.domain.execution_models import IncompleteAcknowledgement
    from voxops.execution.candidate import missing_check_ids

    ctx, location = prepared(tmp_path)
    review = setup_execution(ctx, location, incomplete=True)
    if change == "missing_attempt":
        ctx.run.execution.attempts.pop("startup")
        ctx.run.execution.attempts.pop("probe")
    else:
        ctx.run.execution.attempts["startup"][0].diagnostics = []
    acknowledgement = IncompleteAcknowledgement(
        review_id=review.review_id,
        review_digest=review.digest,
        attestation_digest=review.attestation_digest,
        missing_check_ids=missing_check_ids(review.execution_attestation),
        acknowledged=True,
    )
    approve_review(ctx.run, review.review_id, review.digest, "approve", acknowledgement)
    with pytest.raises(AppError, match="recorded runtime failure"):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""


def test_rechecks_verification_immediately_before_push(tmp_path, monkeypatch):
    ctx, location = prepared(tmp_path)
    setup_execution(ctx, location)
    approve(ctx.run, "artifact_publication")
    original = ctx.executor.scan_history

    def invalidate(path, sha):
        original(path, sha)
        ctx.run.execution.env_schema.revision += 1

    monkeypatch.setattr(ctx.executor, "scan_history", invalidate)
    with pytest.raises(AppError, match="verification"):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""
