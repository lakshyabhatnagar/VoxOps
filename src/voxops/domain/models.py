from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Why: Prevent unchecked fields from silently entering versioned contracts.
# How: Share strict extra-field rejection and assignment validation across domain objects.
class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# Why: Bound framework identifiers used by adapters and downstream consumers.
# How: Serialize a small stable string enumeration.
class Framework(StrEnum):
    FASTAPI = "fastapi"
    VITE = "react_vite"
    NEXT = "nextjs"


# Why: Distinguish independently deployable behaviors without forcing frontend/backend pairs.
# How: Enumerate supported runtime roles, including uncertain and future worker roles.
class Role(StrEnum):
    HTTP = "http_service"
    STATIC = "static_frontend"
    SSR = "server_rendered_web"
    FULLSTACK = "fullstack_web"
    WORKER = "worker"
    UNKNOWN = "unknown"


# Why: Separate proof from inference while keeping source values out of results.
# How: Record relative paths, fixed sanitized field descriptions and bounded confidence.
class Evidence(Model):
    path: str
    kind: str
    field: str
    confidence: float = Field(ge=0, le=1)
    source: Literal["deterministic", "llm"] = "deterministic"


# Why: Describe needed configuration without ever storing configuration values.
# How: Keep names, lifecycle, exposure and presence as typed metadata only.
class ConfigRequirement(Model):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    necessity: Literal["required", "optional", "unknown"] = "unknown"
    stage: Literal["build_time", "runtime", "unknown"] = "unknown"
    visibility: Literal["public", "secret", "unknown"] = "unknown"
    presence: Literal["present", "example_only", "referenced", "unknown"] = "unknown"


# Why: Represent candidate URL wiring without inventing deployed dependencies.
# How: Require producer, consumer and evidence while retaining unverified status.
class UnitDependencyCandidate(Model):
    producer_unit: str
    consumer_unit: str
    purpose: str
    destination_config_key: str
    phase: Literal["build_time", "runtime", "unknown"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["candidate", "verified"] = "candidate"


# Why: Preserve package and startup requirements independently of any cloud adapter.
# How: Describe language version, manager, dependency files and existing commands.
class RuntimeSpec(Model):
    language: Literal["python", "javascript"]
    version: str | None = None
    package_manager: str | None = None
    dependency_files: list[str] = Field(default_factory=list)
    build_command: str | None = None
    start_command: str | None = None
    command_source: Literal["explicit", "inferred", "unknown"] = "unknown"
    output_directory: str | None = None
    workspace_root: str | None = None
    lockfile_path: str | None = None


# Why: Capture an adapter's deterministic classification and remaining uncertainty.
# How: Pair framework and root with evidence, role and explicit open questions.
class FrameworkDetection(Model):
    framework: Framework
    root_path: str
    role_candidate: Role
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)
    unresolved_questions: list[str] = Field(default_factory=list)


# Why: Provide a stable deployable-unit contract for future artifact workers.
# How: Bind deterministic IDs, runtime metadata and value-free configuration to each unit.
class DeployableUnit(Model):
    id: str
    name: str
    root_path: str
    role: Role
    framework: Framework
    language: Literal["python", "javascript"]
    runtime: RuntimeSpec
    config_requirements: list[ConfigRequirement] = Field(default_factory=list)
    dependency_candidates: list[UnitDependencyCandidate] = Field(default_factory=list)
    shared_packages: list[str] = Field(default_factory=list)
    detection: FrameworkDetection


# Why: Report Git prerequisites without exposing remotes or local modifications' contents.
# How: Return inspection metadata and a precise initialized/HEAD status.
class GitMetadata(Model):
    status: Literal["uninitialized", "missing_initial_commit", "committed", "git_unavailable"]
    branch: str | None = None
    head: str | None = None
    dirty: bool = False
    has_remotes: bool = False
    detached: bool = False
    nested_path: bool = False


# Why: Summarize manifests safely for detection and optional model context.
# How: Retain normalized dependency names and redacted commands, never arbitrary JSON fields.
class ManifestSummary(Model):
    path: str
    root_path: str
    kind: Literal["node", "python"]
    dependencies: list[str] = Field(default_factory=list)
    scripts: dict[str, str] = Field(default_factory=dict)
    package_manager: str | None = None
    runtime_version: str | None = None
    workspace: bool = False
    local_dependencies: list[str] = Field(default_factory=list)
    package_name: str | None = None
    build_output_directory: str | None = None
    build_output_dynamic: bool = False
    server_build: bool = False


# Why: Keep the bounded scanner result reproducible and usable independently of detection.
# How: Collect relative file metadata and safe facts with input revision fingerprints.
class RepoScan(Model):
    id: str
    scanned_at: datetime
    scanner_version: str = "0.1.0"
    repo_fingerprint: str
    display_name: str
    git: GitMetadata
    relevant_files: list[str]
    shallow_tree: list[str]
    manifests: list[ManifestSummary]
    config_keys: dict[str, list[ConfigRequirement]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    input_fingerprint: str
    workspace_roots: list[str] = Field(default_factory=list)


# Why: Account for optional model use without saving prompts or credentials.
# How: Record outcome, model identity and available token/request totals.
class AIUsage(Model):
    status: Literal["skipped", "unavailable", "failed", "used"] = "skipped"
    model: str | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# Why: Expose one stable aggregate result for APIs, CLI and persistence.
# How: Combine scanner facts, validated units and unresolved findings with usage metadata.
class AnalysisResult(Model):
    schema_version: Literal["1"] = "1"
    scan: RepoScan
    detected_units: list[DeployableUnit]
    dependencies: list[UnitDependencyCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    ai_usage: AIUsage = Field(default_factory=AIUsage)
    deterministic_only: bool = True


# Why: Track only application-owned worktrees and their committed source revision.
# How: Persist lifecycle and immutable branch/location ownership metadata.
class WorktreeRecord(Model):
    id: str
    source_repo_fingerprint: str
    source_commit_sha: str
    branch: str
    location: str
    created_at: datetime
    status: Literal["creating", "active", "creation_failed", "cleanup_failed", "removed"] = "active"
    dirty: bool = False
    warnings: list[str] = Field(default_factory=list)


# Why: Keep voice transport separate from orchestration and audio retention.
# How: Pass ordinary text and optional provider metadata in a typed object.
class VoiceTranscript(Model):
    text: str
    language: str | None = None
    provider: Literal["transcript_stub", "gnani"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    request_id: str
    error_category: str | None = None
