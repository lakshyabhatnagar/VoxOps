import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from voxops.artifacts.security import redact, safe_read
from voxops.artifacts.validators import _ignored
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactPlan
from voxops.domain.execution_models import (
    LEVEL_CHECKS,
    EnvFormSchema,
    ExecutionAttempt,
    ExecutionDiagnostic,
    ExecutionPlan,
    OperatorExternalBlocker,
    UnitVerification,
)
from voxops.execution.environment import SecretSession


# Identify the existing Stage 2 file task allowed to repair a specific execution failure.
# Carry the short feedback explaining what that file needs to correct.
# This chooses an already-owned task rather than authorizing edits anywhere in the app.
@dataclass(frozen=True)
class RepairTarget:
    task_id: str
    feedback: str


# Keep the revised app result, attempt record, and user's blocker declaration together.
# These records describe the same unavailable external dependency.
# Returning them together lets the caller save a consistent explanation for incomplete
# verification.
@dataclass(frozen=True)
class OperatorBlockerResult:
    unit: UnitVerification
    attempt: ExecutionAttempt
    declaration: OperatorExternalBlocker


# Find which known candidate paths are explicitly mentioned in diagnostic text.
# Match complete paths rather than accepting arbitrary text that merely contains similar letters.
# Return only those known paths to help select a narrowly scoped repair task.
def mentioned_paths(evidence: list[str], paths: list[str]) -> list[str]:
    text = "\n".join(evidence)
    return [
        path for path in paths if re.search(rf"(?<![\w./-]){re.escape(path)}(?![\w./-])", text)
    ][:20]


# Check whether a user's external-service explanation matches the blocked runtime checks.
# Require a passed build and a configured dependency belonging to this app.
# Create an external-dependency diagnostic only when the missing checks and declaration agree.
def operator_external_blocker(
    declaration: OperatorExternalBlocker, unit: UnitVerification, schema: EnvFormSchema
) -> ExecutionDiagnostic | None:
    passed = {kind for kind, check in unit.checks.items() if check.status == "passed"}
    missing = set(LEVEL_CHECKS[unit.required_level]) - passed
    configured = any(
        item.name == declaration.dependency_name
        and item.resolution in {"existing_env", "supplied", "default"}
        and any(c.unit_id == unit.unit_id and c.phase == "runtime" for c in item.consumers)
        for item in schema.requirements
    )
    if (
        declaration.unit_id != unit.unit_id
        or unit.status != "blocked"
        or not configured
        or "build" not in passed
        or missing != set(declaration.checks)
        or unit.diagnostics
        or any(check.status == "failed" for check in unit.checks.values())
    ):
        return None
    stage = "startup" if "startup" in missing else "probe"
    return ExecutionDiagnostic(
        diagnostic_id=declaration.blocker_id,
        unit_id=unit.unit_id,
        task_id=f"{unit.unit_id}:{stage}",
        category="EXTERNAL_SERVICE_UNAVAILABLE",
        stage=stage,
        message=f"Operator recorded an external blocker for {declaration.dependency_name}.",
        evidence=[f"Operator blocker {declaration.blocker_id} identifies the dependency."],
        dependency_name=declaration.dependency_name,
        external_signal="operator",
    )


# Check an operator declaration against a specific completed failed attempt.
# Hide configuration values from the explanation before creating revised records.
# Mark verification incomplete only when the declared dependency accounts for exactly the missing
# runtime checks.
def normalize_operator_blocker(
    declaration: OperatorExternalBlocker,
    unit: UnitVerification,
    schema: EnvFormSchema,
    attempt: ExecutionAttempt,
    session: SecretSession,
) -> OperatorBlockerResult | None:
    missing = set(LEVEL_CHECKS[unit.required_level]) - {
        kind for kind, check in unit.checks.items() if check.status == "passed"
    }
    if (
        unit.status != "failed"
        or missing != set(declaration.checks)
        or len(unit.diagnostics) != 1
        or attempt.unit_id != unit.unit_id
        or attempt.status != "failed"
        or attempt.ended_at is None
        or attempt.oom_killed
        or any(kind not in unit.checks or unit.checks[kind].attempt_id != attempt.attempt_id
               for kind in missing)
    ):
        return None
    original = unit.diagnostics[0]
    if (
        original not in attempt.diagnostics
        or original.task_id != attempt.task_id
        or original.category not in {"UNKNOWN_RUNTIME_FAILURE", "TIMEOUT"}
        or any(original.diagnostic_id not in unit.checks[kind].diagnostic_ids for kind in missing)
    ):
        return None
    explanation = declaration.explanation
    for phase in ("build", "runtime"):
        for visibility in ("public", "secret", "unknown"):
            for value in session.values_for(unit.unit_id, phase, visibility).values():
                if value:
                    explanation = explanation.replace(value, "[REDACTED]")
    declaration = declaration.model_copy(
        update={"explanation": redact(session.redact(explanation))[:1000]}
    )
    normalized = unit.model_copy(deep=True)
    normalized.status, normalized.diagnostics = "blocked", []
    for kind in missing:
        normalized.checks[kind].status = "blocked"
    diagnostic = operator_external_blocker(declaration, normalized, schema)
    if diagnostic is None or diagnostic.task_id != attempt.task_id:
        return None
    revised_attempt = attempt.model_copy(deep=True)
    revised_attempt.diagnostics.append(diagnostic)
    normalized.diagnostics = [diagnostic]
    for kind in missing:
        normalized.checks[kind].diagnostic_ids = [diagnostic.diagnostic_id]
    normalized.status = "verification_incomplete"
    return OperatorBlockerResult(
        unit=UnitVerification.model_validate(normalized.model_dump()),
        attempt=ExecutionAttempt.model_validate(revised_attempt.model_dump()),
        declaration=declaration,
    )


# Look for a runtime failure tied to a declared required service configuration name.
# Recognize supported connection, DNS, and authentication signals in the same evidence line.
# Return an external-service diagnostic only when the evidence supports that explanation.
def external_blocker(
    diagnostic: ExecutionDiagnostic, schema: EnvFormSchema, evidence: list[str]
) -> ExecutionDiagnostic | None:
    if diagnostic.stage not in {"startup", "probe"} or diagnostic.category not in {
        "UNKNOWN_RUNTIME_FAILURE",
        "INVALID_ARTIFACT",
    }:
        return None
    signals = {
        "dns": r"\b(?:getaddrinfo|enotfound|eai_again|name resolution)\b",
        "connection": r"\b(?:econnrefused|connection refused|connection reset|econnreset)\b",
        "authentication": r"\b(?:authentication failed|invalid credentials|access denied)\b",
    }
    for requirement in schema.requirements:
        if (
            requirement.necessity != "required"
            or requirement.resolution not in {"existing_env", "supplied", "default"}
            or not any(
                c.unit_id == diagnostic.unit_id and c.phase == "runtime"
                for c in requirement.consumers
            )
        ):
            continue
        for line in "\n".join(evidence).splitlines():
            if not re.search(rf"(?<!\w){re.escape(requirement.name)}(?!\w)", line, re.I):
                continue
            for signal, pattern in signals.items():
                if re.search(pattern, line, re.I):
                    return ExecutionDiagnostic(
                        diagnostic_id=uuid4().hex,
                        unit_id=diagnostic.unit_id,
                        task_id=diagnostic.task_id,
                        category="EXTERNAL_SERVICE_UNAVAILABLE",
                        stage=diagnostic.stage,
                        message=f"Declared dependency {requirement.name} is unavailable.",
                        evidence=[f"Runtime reported {signal} failure for {requirement.name}."],
                        dependency_name=requirement.name,
                        external_signal=signal,
                    )
    return None


# Choose a narrowly supported repair from the existing Stage 2 tasks.
# Handle approved bind-address source fixes or container COPY/input failures with identifiable
# ownership.
# Return no target when the failure does not justify editing one of those planned files.
def repair_target(
    diagnostic: ExecutionDiagnostic,
    plan: ExecutionPlan,
    artifacts: ArtifactPlan,
    root: Path | None = None,
) -> RepairTarget | None:
    unit = plan.units.get(diagnostic.unit_id or "")
    evidence = " ".join([diagnostic.message, *diagnostic.evidence]).lower()
    if (
        unit is not None
        and diagnostic.stage in {"build", "startup", "probe"}
        and diagnostic.task_id == f"{unit.unit_id}:{diagnostic.stage}"
        and diagnostic.category in {"BUILD_FAILED", "INVALID_ARTIFACT", "UNKNOWN_RUNTIME_FAILURE"}
        and re.search(r"\beaddrnotavail\b|(?:listen|bind).{0,40}(?:host|port)", evidence)
    ):
        owners = [
            task
            for task in artifacts.tasks.values()
            if task.artifact_type == "source_patch"
            and task.approval_class == "source_patch"
            and task.existing_hash
            and task.target_path in diagnostic.paths
            and PurePosixPath(task.target_path).is_relative_to(PurePosixPath(unit.root_path))
        ]
        if len(owners) == 1:
            return RepairTarget(
                owners[0].task_id,
                "The runtime reported a bind or listen failure at the approved source path. "
                "Apply only the approved deployment correction.",
            )
    if (
        unit is None
        or unit.strategy != "container"
        or diagnostic.stage != "build"
        or diagnostic.category not in {"BUILD_FAILED", "INVALID_ARTIFACT"}
        or diagnostic.task_id != f"{unit.unit_id}:build"
    ):
        return None
    if not ("copy" in evidence and ("not found" in evidence or "checksum" in evidence)):
        return None
    owners = [
        task
        for task in artifacts.tasks.values()
        if task.unit_id == unit.unit_id
        and task.artifact_type == "dockerfile"
        and task.target_path == unit.dockerfile_path
    ]
    if len(owners) != 1:
        return None
    if root is not None:
        ignores = [
            task
            for task in artifacts.tasks.values()
            if task.artifact_type == "dockerignore" and task.build_context == unit.context_path
        ]
        if len(ignores) == 1:
            try:
                data = safe_read(root, ignores[0].target_path)
                if data is not None:
                    content = data.decode()
                    for path in owners[0].required_paths:
                        if (
                            path.lower() in mentioned_paths([evidence], [path.lower()])
                            and _ignored(content, path)
                            and safe_read(root, path) is not None
                        ):
                            return RepairTarget(
                                ignores[0].task_id,
                                "An existing required input is excluded by the owned dockerignore. "
                                "Allow that planned input while retaining secret exclusions.",
                            )
            except (AppError, UnicodeError):
                return None
    return RepairTarget(
        owners[0].task_id,
        "The container build reported a COPY path or checksum failure. "
        "Check the planned build context and existing inputs.",
    )
