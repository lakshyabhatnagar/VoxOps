"""Value-free execution records. Secret sessions never belong in these models."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from voxops.domain.models import Model

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Strategy = Literal["container", "static", "native"]
ExecutionStatus = Literal[
    "awaiting_verification",
    "waiting_for_env",
    "verifying",
    "passed",
    "verification_incomplete",
    "failed",
    "blocked",
    "cancelled",
    "interrupted",
]
VerificationLevel = Literal[
    "not_attempted",
    "build_passed",
    "process_started",
    "http_responded",
    "application_health_confirmed",
    "static_output_verified",
    "static_http_responded",
]
CheckKind = Literal["build", "startup", "http", "health", "static_output", "static_http"]
CHECK_OPERATIONS = {
    "build": "build",
    "startup": "startup",
    "http": "probe",
    "health": "probe",
    "static_output": "static_output",
    "static_http": "probe",
}
LEVEL_CHECKS: dict[str, tuple[str, ...]] = {
    "not_attempted": (),
    "build_passed": ("build",),
    "process_started": ("build", "startup"),
    "http_responded": ("build", "startup", "http"),
    "application_health_confirmed": ("build", "startup", "http", "health"),
    "static_output_verified": ("build", "static_output"),
    "static_http_responded": ("build", "static_output", "static_http"),
}


class EnvEvidence(Model):
    path: str
    line: int | None = Field(default=None, ge=1)
    kind: Literal["stage1", "python", "javascript", "example", "operator"]
    detail: str = Field(default="", max_length=500)


class EnvConsumer(Model):
    unit_id: str
    phase: Literal["build", "runtime"]


class EnvRequirement(Model):
    field_id: str = Field(min_length=1)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    consumers: list[EnvConsumer] = Field(min_length=1)
    scope: str = "."
    necessity: Literal["required", "optional", "unknown"]
    default_kind: Literal["absent", "literal", "none", "dynamic"] = "absent"
    visibility: Literal["public", "secret", "unknown"] = "unknown"
    resolution: Literal["missing", "existing_env", "default", "supplied", "ambiguous"] = "missing"
    value_type: Literal["string", "integer", "port", "url", "boolean"] = "string"
    max_length: int = Field(default=8192, ge=1, le=65536)
    evidence: list[EnvEvidence] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_resolution(self) -> "EnvRequirement":
        keys = {(item.unit_id, item.phase) for item in self.consumers}
        if len(keys) != len(self.consumers):
            raise ValueError("Environment consumers must be distinct")
        if self.resolution == "default" and self.default_kind not in {"literal", "none"}:
            raise ValueError("Default resolution requires a known static default")
        return self


class EnvFormSchema(Model):
    schema_version: Literal["1"] = "1"
    source_digest: Digest
    revision: int = Field(default=1, ge=1)
    requirements: list[EnvRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_fields(self) -> "EnvFormSchema":
        if len({item.field_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("Environment field identities must be unique")
        return self


class ProbeSpec(Model):
    path: str = Field(default="/", pattern=r"^/[^\s]*$", max_length=512)
    port: int = Field(ge=1, le=65535)
    health_contract: bool = False
    success_statuses: list[int] = Field(default_factory=lambda: [200], min_length=1)

    @model_validator(mode="after")
    def safe_probe(self) -> "ProbeSpec":
        if self.path.startswith("//") or any(char in self.path for char in "?#\\"):
            raise ValueError("Probe must be a local absolute path without query or fragment")
        if any(status < 200 or status >= 300 for status in self.success_statuses):
            raise ValueError("Health success statuses must be successful HTTP responses")
        return self


class UnitExecutionSpec(Model):
    unit_id: str
    strategy: Strategy
    context_path: str = "."
    root_path: str = "."
    required_paths: list[str] = Field(default_factory=list)
    dockerfile_path: str | None = None
    builder_image: str | None = None
    install_argv: list[str] = Field(default_factory=list)
    build_argv: list[str] = Field(default_factory=list)
    start_argv: list[str] = Field(default_factory=list)
    output_directory: str | None = None
    probe: ProbeSpec | None = None
    required_level: VerificationLevel

    @model_validator(mode="after")
    def real_verification_target(self) -> "UnitExecutionSpec":
        if self.required_level in {"not_attempted", "build_passed", "process_started"}:
            raise ValueError("A unit requires runtime or static output verification")
        static = self.required_level in {"static_output_verified", "static_http_responded"}
        if (self.strategy == "static") != static:
            raise ValueError("Static units require static output verification")
        if static and not self.output_directory:
            raise ValueError("Static verification requires an output directory")
        if self.required_level in {
            "http_responded",
            "application_health_confirmed",
            "static_http_responded",
        }:
            if self.probe is None:
                raise ValueError("HTTP verification requires a probe")
        if self.required_level == "application_health_confirmed" and not self.probe.health_contract:
            raise ValueError("Application health requires an explicit health contract")
        return self


class ExecutionTask(Model):
    task_id: str
    unit_id: str
    operation: Literal["build", "startup", "probe", "static_output"]
    depends_on: list[str] = Field(default_factory=list)
    input_digest: Digest


class ExecutionPlan(Model):
    schema_version: Literal["1"] = "1"
    plan_id: str
    artifact_plan_id: str
    artifact_plan_digest: Digest
    units: dict[str, UnitExecutionSpec] = Field(min_length=1)
    tasks: dict[str, ExecutionTask] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_graph(self) -> "ExecutionPlan":
        if any(key != unit.unit_id for key, unit in self.units.items()):
            raise ValueError("Execution unit identity mismatch")
        for key, task in self.tasks.items():
            if key != task.task_id or task.unit_id not in self.units:
                raise ValueError("Execution task identity mismatch")
            if set(task.depends_on) - self.tasks.keys():
                raise ValueError("Unknown execution task dependency")
        if {task.unit_id for task in self.tasks.values()} != self.units.keys():
            raise ValueError("Execution tasks must cover every unit")
        pending = {key: set(task.depends_on) for key, task in self.tasks.items()}
        while pending:
            ready = {key for key, dependencies in pending.items() if not dependencies}
            if not ready:
                raise ValueError("Execution task dependency cycle")
            pending = {key: deps - ready for key, deps in pending.items() if key not in ready}
        for unit_id, unit in self.units.items():
            operations = {
                task.operation: task for task in self.tasks.values() if task.unit_id == unit_id
            }
            required = {CHECK_OPERATIONS[kind] for kind in LEVEL_CHECKS[unit.required_level]}
            if set(operations) != required or len(operations) != sum(
                task.unit_id == unit_id for task in self.tasks.values()
            ):
                raise ValueError(
                    "Execution plan must contain required verification operations once"
                )
            for operation, dependency in (
                ("startup", "build"),
                ("static_output", "build"),
                ("probe", "static_output" if unit.strategy == "static" else "startup"),
            ):
                if operation not in operations:
                    continue
                ancestors = set()
                queue = list(operations[operation].depends_on)
                while queue:
                    task_id = queue.pop()
                    if task_id not in ancestors:
                        ancestors.add(task_id)
                        queue.extend(self.tasks[task_id].depends_on)
                if operations[dependency].task_id not in ancestors:
                    raise ValueError("Verification operations must wait for their required inputs")
        return self


class CandidateIdentity(Model):
    artifact_run_id: str
    execution_id: str
    artifact_plan_digest: Digest
    execution_plan_digest: Digest
    source_digest: Digest
    inventory_digest: Digest
    env_schema_digest: Digest
    policy_digest: Digest
    digest: Digest


class ExecutionDiagnostic(Model):
    diagnostic_id: str
    unit_id: str | None = None
    task_id: str | None = None
    category: Literal[
        "DOCKER_UNAVAILABLE",
        "DOCKER_PERMISSION",
        "BUILDKIT_UNAVAILABLE",
        "BUILDKIT_ISOLATION_UNAVAILABLE",
        "BUILD_FAILED",
        "INVALID_ARTIFACT",
        "MISSING_ENV",
        "AMBIGUOUS_ENV",
        "BUILD_SECRET_INJECTION_UNSUPPORTED",
        "EXTERNAL_SERVICE_UNAVAILABLE",
        "UNKNOWN_RUNTIME_FAILURE",
        "TIMEOUT",
        "RESOURCE_LIMIT",
        "CANCELLED",
        "INTERRUPTED",
        "REGISTRY_UNAVAILABLE",
    ]
    stage: Literal["preflight", "environment", "build", "startup", "probe", "static_output"]
    message: str = Field(max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    paths: list[str] = Field(default_factory=list, max_length=20)
    dependency_name: str | None = None
    external_signal: Literal["dns", "connection", "authentication", "operator"] | None = None

    @model_validator(mode="after")
    def grounded_external_blocker(self) -> "ExecutionDiagnostic":
        if self.category == "EXTERNAL_SERVICE_UNAVAILABLE" and (
            not self.dependency_name
            or not self.external_signal
            or not self.evidence
            or self.stage not in {"startup", "probe"}
        ):
            raise ValueError("External dependency blocker needs linked runtime evidence")
        return self


class CheckResult(Model):
    check_id: str
    kind: CheckKind
    status: Literal["passed", "failed", "blocked", "not_attempted"]
    evidence: list[str] = Field(default_factory=list, max_length=20)
    diagnostic_ids: list[str] = Field(default_factory=list)
    attempt_id: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)

    @model_validator(mode="after")
    def truthful_check(self) -> "CheckResult":
        if self.status == "passed" and not self.evidence:
            raise ValueError("Passed checks require observed evidence")
        if self.status == "passed" and not self.attempt_id:
            raise ValueError("Passed checks require a recorded execution attempt")
        if self.status == "passed" and self.kind in {"http", "health", "static_http"}:
            if self.http_status is None or self.http_status >= 500:
                raise ValueError("Passed HTTP checks require a non-server-error response")
            if self.kind in {"health", "static_http"} and not 200 <= self.http_status < 300:
                raise ValueError("Health/static HTTP checks require a successful response")
        return self


class UnitVerification(Model):
    unit_id: str
    input_digest: Digest
    required_level: VerificationLevel
    achieved_level: VerificationLevel = "not_attempted"
    status: Literal["passed", "verification_incomplete", "failed", "blocked", "cancelled"]
    checks: dict[CheckKind, CheckResult]
    diagnostics: list[ExecutionDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def truthful_status(self) -> "UnitVerification":
        if any(
            key != item.kind or item.check_id != f"{self.unit_id}:{key}"
            for key, item in self.checks.items()
        ):
            raise ValueError("Check identity must match unit and kind")
        passed = {key for key, check in self.checks.items() if check.status == "passed"}
        required = set(LEVEL_CHECKS[self.required_level])
        achieved = set(LEVEL_CHECKS[self.achieved_level])
        if not achieved <= passed:
            raise ValueError("Claimed verification level lacks successful checks")
        if self.status in {"passed", "verification_incomplete"} and (
            self.required_level in {"not_attempted", "build_passed"} or "build" not in passed
        ):
            raise ValueError("Publishable verification requires build and runtime/static checks")
        if self.status == "passed" and (not required <= achieved or not required <= passed):
            raise ValueError("Passed verification lacks required successful checks")
        if self.status in {"passed", "verification_incomplete"} and any(
            check.status == "failed" for check in self.checks.values()
        ):
            raise ValueError("Publishable verification cannot contain failed checks")
        if self.status == "verification_incomplete":
            missing = required - passed
            blockers = {
                item.diagnostic_id
                for item in self.diagnostics
                if item.category == "EXTERNAL_SERVICE_UNAVAILABLE" and item.unit_id == self.unit_id
            }
            if not missing or missing - {"startup", "http", "health"} or not blockers:
                raise ValueError("Incomplete verification requires external runtime blockers")
            if any(
                kind not in self.checks or not (set(self.checks[kind].diagnostic_ids) & blockers)
                for kind in missing
            ):
                raise ValueError("Every missing check needs grounded external dependency evidence")
        return self


class ExecutionAttempt(Model):
    attempt_id: str
    task_id: str
    unit_id: str
    input_digest: Digest
    # At most four content revisions, with one transient reattempt each.
    attempt_number: int = Field(ge=1, le=8)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    status: Literal["running", "passed", "failed", "blocked", "cancelled", "interrupted"]
    exit_code: int | None = None
    diagnostics: list[ExecutionDiagnostic] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, max_length=30)


class OwnedResourceIntent(Model):
    resource_id: str | None = None
    name: str
    kind: Literal["container", "image", "network", "volume", "directory"]
    task_id: str
    ownership_token: str
    state: Literal["creating", "active", "removing", "removed"] = "creating"


class ExecutionAttestation(Model):
    schema_version: Literal["1"] = "1"
    identity: CandidateIdentity
    status: Literal["passed", "verification_incomplete", "failed", "blocked", "cancelled"]
    units: dict[str, UnitVerification] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deployment_executed: Literal[False] = False
    digest: str = ""

    @model_validator(mode="after")
    def truthful_aggregate(self) -> "ExecutionAttestation":
        if any(key != item.unit_id for key, item in self.units.items()):
            raise ValueError("Attestation unit identity mismatch")
        statuses = {unit.status for unit in self.units.values()}
        if self.status == "passed" and statuses != {"passed"}:
            raise ValueError("Passed attestation requires every unit to pass")
        if self.status == "verification_incomplete" and (
            "verification_incomplete" not in statuses
            or statuses - {"passed", "verification_incomplete"}
        ):
            raise ValueError(
                "Incomplete attestation permits only passed or externally blocked units"
            )
        return self


class IncompleteAcknowledgement(Model):
    review_id: str
    review_digest: Digest
    attestation_digest: Digest
    missing_check_ids: list[str] = Field(min_length=1)
    acknowledged: Literal[True]


class ExecutionRun(Model):
    schema_version: Literal["1"] = "1"
    execution_id: str
    artifact_run_id: str
    epoch: str
    owner_process: str | None = None
    status: ExecutionStatus = "awaiting_verification"
    plan: ExecutionPlan
    env_schema: EnvFormSchema
    identity: CandidateIdentity
    attempts: dict[str, list[ExecutionAttempt]] = Field(default_factory=dict)
    resources: list[OwnedResourceIntent] = Field(default_factory=list)
    attestation: ExecutionAttestation | None = None

    @model_validator(mode="after")
    def linked_identity(self) -> "ExecutionRun":
        if self.identity.execution_id != self.execution_id or (
            self.identity.artifact_run_id != self.artifact_run_id
        ):
            raise ValueError("Execution candidate linkage mismatch")
        if self.attestation and self.attestation.identity != self.identity:
            raise ValueError("Execution attestation candidate mismatch")
        for task_id, attempts in self.attempts.items():
            if task_id not in self.plan.tasks or len(attempts) > 8:
                raise ValueError("Execution attempts must belong to a bounded planned task")
            task = self.plan.tasks[task_id]
            if any(
                attempt.task_id != task_id or attempt.unit_id != task.unit_id
                for attempt in attempts
            ) or len({attempt.attempt_id for attempt in attempts}) != len(attempts):
                raise ValueError("Execution attempt identity mismatch")
        return self
