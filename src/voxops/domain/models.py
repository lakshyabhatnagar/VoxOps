from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Provides common rules for the app's data objects.
# Rejects unknown fields and checks values when fields are changed.
class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# Lists the app frameworks that the scanner recognizes.
# Uses the stored names for FastAPI, React with Vite, and Next.js.
class Framework(StrEnum):
    FASTAPI = "fastapi"
    VITE = "react_vite"
    NEXT = "nextjs"


# Lists the ways a detected app can serve or process work.
# Includes web, static, worker, and unknown roles.
class Role(StrEnum):
    HTTP = "http_service"
    STATIC = "static_frontend"
    SSR = "server_rendered_web"
    FULLSTACK = "fullstack_web"
    WORKER = "worker"
    UNKNOWN = "unknown"


# Records a fact used to identify an app or its requirements.
# Includes the source file, description, confidence, and whether AI supplied it.
class Evidence(Model):
    path: str
    kind: str
    field: str
    confidence: float = Field(ge=0, le=1)
    source: Literal["deterministic", "llm"] = "deterministic"


# Describes a configuration setting an app may need.
# Records its name, timing, visibility, and presence without storing its value.
class ConfigRequirement(Model):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    necessity: Literal["required", "optional", "unknown"] = "unknown"
    stage: Literal["build_time", "runtime", "unknown"] = "unknown"
    visibility: Literal["public", "secret", "unknown"] = "unknown"
    presence: Literal["present", "example_only", "referenced", "unknown"] = "unknown"


# Describes a possible connection between two detected app units.
# Records the configuration key, supporting evidence, confidence, and verification status.
class UnitDependencyCandidate(Model):
    producer_unit: str
    consumer_unit: str
    purpose: str
    destination_config_key: str
    phase: Literal["build_time", "runtime", "unknown"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["candidate", "verified"] = "candidate"


# Describes what an app needs to build and start.
# Records its language, package files, commands, workspace, and output folder.
class RuntimeSpec(Model):
    language: Literal["python", "javascript"]
    version: str | None = None
    package_manager: str | None = None
    dependency_files: list[str] = Field(default_factory=list)
    build_command: str | None = None
    start_command: str | None = None
    prerequisite_commands: list[str] = Field(default_factory=list)
    command_source: Literal["explicit", "inferred", "unknown"] = "unknown"
    output_directory: str | None = None
    workspace_root: str | None = None
    lockfile_path: str | None = None


# Records how the scanner identified an app framework and role.
# Includes the app folder, supporting evidence, confidence, and unanswered questions.
class FrameworkDetection(Model):
    framework: Framework
    root_path: str
    role_candidate: Role
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)
    unresolved_questions: list[str] = Field(default_factory=list)


# Describes one detected part of an app that can be deployed.
# Combines its identity, framework, runtime, configuration needs, and detection evidence.
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


# Records the state of Git in the scanned project.
# Includes the branch, commit, local changes, remotes, and repository status.
class GitMetadata(Model):
    status: Literal["uninitialized", "missing_initial_commit", "committed", "git_unavailable"]
    branch: str | None = None
    head: str | None = None
    dirty: bool = False
    has_remotes: bool = False
    detached: bool = False
    nested_path: bool = False


# Summarizes a package file found in the project.
# Keeps dependency names, scripts, workspace details, and build information.
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


# Records a discovered command for starting an app.
# Includes the source path and commands that need to run first.
class StartupCommand(Model):
    path: str
    command: str
    prerequisite_commands: list[str] = Field(default_factory=list)


# Holds the files and facts collected while scanning a project.
# Includes Git details, package summaries, configuration names, warnings, and checksums.
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
    startup_commands: list[StartupCommand] = Field(default_factory=list)


# Records whether an AI model was used during analysis.
# Keeps the model name and counts of requests and tokens without saving prompts.
class AIUsage(Model):
    status: Literal["skipped", "unavailable", "failed", "used"] = "skipped"
    model: str | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# Combines the project scan with the detected app units and possible connections.
# Includes warnings, unanswered questions, and information about AI use.
class AnalysisResult(Model):
    schema_version: Literal["1"] = "1"
    scan: RepoScan
    detected_units: list[DeployableUnit]
    dependencies: list[UnitDependencyCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    ai_usage: AIUsage = Field(default_factory=AIUsage)
    deterministic_only: bool = True


# Tracks a project working copy created by the app.
# Records its source commit, branch, location, status, and warnings.
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


# Holds text received from a voice provider.
# Includes the language, confidence, request ID, and any error category.
class VoiceTranscript(Model):
    text: str
    language: str | None = None
    provider: Literal["transcript_stub", "gnani"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    request_id: str
    error_category: str | None = None
