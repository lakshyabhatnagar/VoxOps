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


# Remember where a configuration name was discovered.
# Store the source path, optional line number, and a short explanation.
# This lets the form explain why it asks for a value without storing the value itself.
class EnvEvidence(Model):
    path: str
    line: int | None = Field(default=None, ge=1)
    kind: Literal["stage1", "python", "javascript", "example", "operator"]
    detail: str = Field(default="", max_length=500)


# Name an app that uses an environment variable.
# Record whether that app needs it during the build or while running.
# The session uses this pair to send values only to the operations that need them.
class EnvConsumer(Model):
    unit_id: str
    phase: Literal["build", "runtime"]


# Describe one configuration field in the environment form.
# Keep its name, consumers, expected type, requiredness, and how it is currently resolved.
# Actual values live separately in memory, so this record can be saved without including
# passwords.
class EnvRequirement(Model):
    field_id: str = Field(min_length=1)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    consumers: list[EnvConsumer] = Field(min_length=1)
    scope: str = "."
    case_sensitive: bool = True
    necessity: Literal["required", "optional", "unknown"]
    default_kind: Literal["absent", "literal", "none", "dynamic"] = "absent"
    visibility: Literal["public", "secret", "unknown"] = "unknown"
    resolution: Literal["missing", "existing_env", "default", "supplied", "ambiguous"] = "missing"
    value_type: Literal["string", "integer", "port", "url", "boolean"] = "string"
    max_length: int = Field(default=8192, ge=1, le=65536)
    evidence: list[EnvEvidence] = Field(default_factory=list, max_length=30)

    # Reject repeated consumers for the same field.
    # Also reject a field marked defaulted when no supported default was discovered.
    # This keeps the saved form's status consistent with the evidence used to build it.
    @model_validator(mode="after")
    def valid_resolution(self) -> "EnvRequirement":
        keys = {(item.unit_id, item.phase) for item in self.consumers}
        if len(keys) != len(self.consumers):
            raise ValueError("Environment consumers must be distinct")
        if self.resolution == "default" and self.default_kind not in {"literal", "none"}:
            raise ValueError("Default resolution requires a known static default")
        return self


# Describe the complete environment form for this source version.
# Include discovered fields, selected .env file names, and unresolved scanning questions.
# Save this structure independently from the private values used to fill it.
class EnvFormSchema(Model):
    schema_version: Literal["1"] = "1"
    source_digest: Digest
    revision: int = Field(default=1, ge=1)
    scan_id: str = ""
    ambiguities: list[EnvEvidence] = Field(default_factory=list)
    env_files: dict[str, list[str]] = Field(default_factory=dict)
    requirements: list[EnvRequirement] = Field(default_factory=list)

    # Check that every form field has a different field ID.
    # Repeated names can legitimately exist in different scopes, but their IDs must still differ.
    # Reject duplicates so a submission cannot accidentally address two different fields at once.
    @model_validator(mode="after")
    def distinct_fields(self) -> "EnvFormSchema":
        if len({item.field_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("Environment field identities must be unique")
        return self


# Describe the local HTTP request used to check a server.
# Keep its container port, path, and optional health-success statuses together.
# The driver supplies the allocated host port when the readiness request actually runs.
class ProbeSpec(Model):
    path: str = Field(default="/", pattern=r"^/[^\s]*$", max_length=512)
    port: int = Field(ge=1, le=65535)
    health_contract: bool = False
    success_statuses: list[int] = Field(default_factory=lambda: [200], min_length=1)

    # Reject paths that could redirect the intended request to another address or add extra URL
    # parts.
    # Require explicit health-success statuses to be successful HTTP responses.
    # This keeps the probe's saved contract consistent with the local check the runner performs.
    @model_validator(mode="after")
    def safe_probe(self) -> "ProbeSpec":
        if self.path.startswith("//") or any(char in self.path for char in "?#\\"):
            raise ValueError("Probe must be a local absolute path without query or fragment")
        if any(status < 200 or status >= 300 for status in self.success_statuses):
            raise ValueError("Health success statuses must be successful HTTP responses")
        return self


# Describe how one app will be built and checked.
# Store its selected files, commands, Docker strategy, tools, expected output, and HTTP details.
# The planner fills this record and the runner uses it without guessing new commands mid-run.
class UnitExecutionSpec(Model):
    unit_id: str
    strategy: Strategy
    framework: str = ""
    context_path: str = "."
    root_path: str = "."
    required_paths: list[str] = Field(default_factory=list)
    selected_paths: list[str] | None = None
    install_workdir: str = "/workspace"
    command_workdir: str = "/workspace"
    toolchain: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    source_inventory_digest: Digest | None = None
    port_from_env: bool = False
    dockerfile_path: str | None = None
    builder_image: str | None = None
    install_argv: list[str] = Field(default_factory=list)
    build_argv: list[str] = Field(default_factory=list)
    start_argv: list[str] = Field(default_factory=list)
    output_directory: str | None = None
    probe: ProbeSpec | None = None
    required_level: VerificationLevel

    # Reject a plan that stops at merely building or starting an app.
    # Require static strategies to verify output and server strategies to have the required HTTP
    # details.
    # A saved plan must describe enough checking to support the final review.
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


# Describe one build, startup, HTTP-probe, or static-output job.
# Name its app, the jobs it waits for, and its input comparison code.
# Those dependencies keep readiness checks from running before their required build or startup
# step.
class ExecutionTask(Model):
    task_id: str
    unit_id: str
    operation: Literal["build", "startup", "probe", "static_output"]
    depends_on: list[str] = Field(default_factory=list)
    input_digest: Digest


# Group all apps' execution settings and verification jobs under one plan ID.
# Link that plan to the original Stage 2 file-generation plan.
# The service saves it so attempts and results can be checked against the work that was actually
# requested.
class ExecutionPlan(Model):
    schema_version: Literal["1"] = "1"
    plan_id: str
    artifact_plan_id: str
    artifact_plan_digest: Digest
    units: dict[str, UnitExecutionSpec] = Field(min_length=1)
    tasks: dict[str, ExecutionTask] = Field(min_length=1)

    # Check that jobs refer to real apps and dependencies and do not form a dependency loop.
    # Require each app's verification operations exactly once.
    # Also ensure startup, output, and HTTP jobs wait for the earlier operations they need.
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


# Name the run, execution, and plans that one verification result belongs to.
# Keep comparison codes instead of copies of all source files or secret values.
# The publication checks compare this identity with the files and plans currently being approved.
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


# Describe an observed execution problem in a bounded, structured record.
# Keep its category, stage, app, evidence, and relevant known paths together.
# Repair and review code can then distinguish a file defect from missing configuration or an
# unavailable service.
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

    # Require an external-service failure to identify its dependency and supporting signal.
    # Accept that classification only for startup or HTTP checking with recorded evidence.
    # A build defect cannot become incomplete verification merely by being labeled external.
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


# Describe a user's explanation that a configured external service is unavailable.
# Name the affected app, dependency, and runtime checks that could not be completed.
# This declaration is checked against actual attempt records before it changes a result.
class OperatorExternalBlocker(Model):
    blocker_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    unit_id: str
    dependency_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    explanation: str = Field(min_length=1, max_length=1000)
    checks: list[Literal["startup", "http", "health"]] = Field(min_length=1, max_length=3)

    # Reject a declaration that lists the same missing check more than once.
    # The list must identify distinct startup, HTTP, or health checks.
    # This makes exact comparisons with the app's unverified checks meaningful.
    @model_validator(mode="after")
    def distinct_checks(self) -> "OperatorExternalBlocker":
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("Operator blocker checks must be distinct")
        return self


# Save the result of one specific verification check.
# Link its status and evidence to the attempt that produced them.
# For HTTP checks, keep the observed status code so success can be validated later.
class CheckResult(Model):
    check_id: str
    kind: CheckKind
    status: Literal["passed", "failed", "blocked", "not_attempted"]
    evidence: list[str] = Field(default_factory=list, max_length=20)
    diagnostic_ids: list[str] = Field(default_factory=list)
    attempt_id: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)

    # Reject passed checks that lack evidence or a recorded attempt.
    # Require an appropriate observed status for passed HTTP checks.
    # Health and static HTTP checks need successful responses rather than merely reaching a
    # server.
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


# Summarize the build and runtime or static-output checks for one app.
# Keep its required level, achieved level, individual results, and diagnostics together.
# The service uses this to decide whether the app passed, needs repair, or remains incompletely
# verified.
class UnitVerification(Model):
    unit_id: str
    input_digest: Digest
    required_level: VerificationLevel
    achieved_level: VerificationLevel = "not_attempted"
    status: Literal["passed", "verification_incomplete", "failed", "blocked", "cancelled"]
    checks: dict[CheckKind, CheckResult]
    diagnostics: list[ExecutionDiagnostic] = Field(default_factory=list)

    # Check that each saved check belongs to this app and supports the claimed achieved level.
    # Require every necessary check to pass before the app can be marked passed.
    # Permit incomplete verification only under the supported external-blocker conditions.
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
            if any(item.category != "EXTERNAL_SERVICE_UNAVAILABLE" for item in self.diagnostics):
                raise ValueError("Incomplete verification cannot waive other failures")
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


# Record one actual try at a verification operation.
# Keep its number, input comparison code, timing, result, and bounded evidence.
# Retries append new attempts so the original failure and later correction remain visible.
class ExecutionAttempt(Model):
    attempt_id: str
    task_id: str
    unit_id: str
    input_digest: Digest
    attempt_number: int = Field(ge=1)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    status: Literal["running", "passed", "failed", "blocked", "cancelled", "interrupted"]
    exit_code: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    oom_killed: bool | None = None
    diagnostics: list[ExecutionDiagnostic] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, max_length=30)


# Record a Docker container, image, or volume that this execution owns or intends to create.
# Track its name, eventual ID, ownership token, and cleanup state.
# Recovery can inspect these records after interruption without deleting unrelated Docker
# resources.
class OwnedResourceIntent(Model):
    resource_id: str | None = None
    name: str
    kind: Literal["container", "image", "network", "volume", "directory"]
    task_id: str
    ownership_token: str
    state: Literal["creating", "active", "removing", "removed"] = "creating"


# Collect the per-app verification results for one exact candidate.
# Keep the overall status, creation time, and report comparison code beside the candidate
# identity.
# This is the saved report attached to the final review; it does not represent a cloud deployment.
class ExecutionAttestation(Model):
    schema_version: Literal["1"] = "1"
    identity: CandidateIdentity
    status: Literal["passed", "verification_incomplete", "failed", "blocked", "cancelled"]
    units: dict[str, UnitVerification] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deployment_executed: Literal[False] = False
    digest: str = ""

    # Check that the report's app keys match their contained app IDs.
    # An overall pass requires every app to pass.
    # An incomplete report may contain passed and incompletely verified apps, but not unrelated
    # failures.
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


# Record that the user accepts the exact checks left unverified in a specific review.
# Name both the review and verification report by their comparison codes.
# Changing the review or the missing-check list means this acknowledgement no longer applies.
class IncompleteAcknowledgement(Model):
    review_id: str
    review_digest: Digest
    attestation_digest: Digest
    missing_check_ids: list[str] = Field(min_length=1)
    acknowledged: Literal[True]


# Hold the saved progress for one execution of a Stage 2 candidate.
# Keep the plan, value-free environment form, attempts, results, resource records, and final
# report.
# Actual secret values remain in the separate in-memory session rather than in this saved object.
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
    units: dict[str, UnitVerification] = Field(default_factory=dict)
    previous_plan: ExecutionPlan | None = None
    config_generations: dict[str, str] = Field(default_factory=dict)
    operator_blockers: list[OperatorExternalBlocker] = Field(default_factory=list)
    resources: list[OwnedResourceIntent] = Field(default_factory=list)
    attestation: ExecutionAttestation | None = None

    # Check that the saved run, candidate identity, report, jobs, and attempts refer to each other
    # correctly.
    # Require operator blockers to identify planned checks and configured dependencies.
    # Reject mismatched or duplicated records instead of letting them support publication.
    @model_validator(mode="after")
    def linked_identity(self) -> "ExecutionRun":
        if self.identity.execution_id != self.execution_id or (
            self.identity.artifact_run_id != self.artifact_run_id
        ):
            raise ValueError("Execution candidate linkage mismatch")
        if self.attestation and self.attestation.identity != self.identity:
            raise ValueError("Execution attestation candidate mismatch")
        if len({item.blocker_id for item in self.operator_blockers}) != len(self.operator_blockers):
            raise ValueError("Operator blocker identity must be unique")
        for blocker in self.operator_blockers:
            spec = self.plan.units.get(blocker.unit_id)
            if spec is None or not set(blocker.checks) <= set(LEVEL_CHECKS[spec.required_level]):
                raise ValueError("Operator blocker must name planned runtime checks")
            if not any(
                item.name == blocker.dependency_name
                and item.resolution in {"existing_env", "supplied", "default"}
                and any(
                    c.unit_id == blocker.unit_id and c.phase == "runtime" for c in item.consumers
                )
                for item in self.env_schema.requirements
            ):
                raise ValueError("Operator blocker must name a configured runtime dependency")
        for task_id, attempts in self.attempts.items():
            if task_id not in self.plan.tasks:
                raise ValueError("Execution attempts must belong to a planned task")
            task = self.plan.tasks[task_id]
            if any(
                attempt.task_id != task_id or attempt.unit_id != task.unit_id
                for attempt in attempts
            ) or len({attempt.attempt_id for attempt in attempts}) != len(attempts):
                raise ValueError("Execution attempt identity mismatch")
        return self
