from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Read application paths, limits, and service choices from environment variables.
# Keep each provider's optional key separate and hide key values when settings are printed.
# Check local paths and limits even when no external service has been configured.
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOXOPS_", extra="ignore")
    allowed_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    mongo_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    mongo_database: str = Field(default="voxops", pattern=r"^[A-Za-z0-9_-]{1,63}$")
    active_execution_timeout_minutes: float = Field(default=45, gt=0)
    hitl_idle_timeout_minutes: float = Field(default=30, gt=0)
    workspace_max_lifetime_minutes: float = Field(default=75, gt=0)
    max_repair_cycles: int = Field(default=1, ge=0, le=3)
    llm_call_retries: int = Field(default=1, ge=0, le=2)
    max_depth: int = Field(default=8, ge=1, le=20)
    max_files: int = Field(default=3000, ge=1, le=20000)
    max_file_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    max_context_chars: int = Field(default=64000, ge=1000, le=128000)
    model_timeout: float = Field(default=20, ge=1, le=60)
    model: str = "anthropic:claude-sonnet-4-5"
    artifact_model_timeout: float = Field(default=180, ge=1, le=300)
    artifact_concurrency: int = Field(default=3, ge=1, le=8)
    artifact_max_requests: int = Field(default=60, ge=1, le=300)
    artifact_max_tokens: int = Field(default=150000, ge=1000, le=1000000)
    artifact_task_tokens: int = Field(default=20000, ge=1000, le=100000)
    artifact_staged_bytes: int = Field(default=8388608, ge=131072, le=67108864)
    artifact_optional_tools: bool = False
    execution_max_parallel: int = Field(default=2, ge=1, le=4)
    build_timeout_seconds: float = Field(default=600, ge=10, le=3600)
    startup_timeout_seconds: float = Field(default=90, ge=1, le=300)
    execution_log_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    execution_log_max_lines: int = Field(default=500, ge=10, le=10000)
    execution_build_cpus: float = Field(default=2, ge=0.25, le=16)
    execution_build_memory_mb: int = Field(default=3072, ge=256, le=32768)
    execution_build_pids: int = Field(default=512, ge=32, le=4096)
    execution_runtime_cpus: float = Field(default=1, ge=0.25, le=8)
    execution_runtime_memory_mb: int = Field(default=512, ge=128, le=16384)
    execution_runtime_pids: int = Field(default=128, ge=16, le=2048)
    execution_probe_timeout_seconds: float = Field(default=5, ge=0.1, le=30)
    execution_node_image: str = "node:22-bookworm-slim"
    execution_python_image: str = "python:3.12-slim"
    execution_buildkit_image: str = "moby/buildkit:rootless"
    github_mcp_url: str | None = None
    github_mcp_token: SecretStr | None = None
    git_credential_helper: str | None = None
    git_timeout: float = Field(default=30, ge=1, le=120)
    git_author_name: str = "VoxOps"
    git_author_email: str = "voxops@localhost"
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    nvidia_api_key: SecretStr | None = Field(default=None, validation_alias="NVIDIA_API_KEY")

    @field_validator("allowed_roots")
    # Checks the directories allowed for workspace access.
    # Requires at least one existing directory and resolves its path.
    def valid_roots(cls, roots: list[Path]) -> list[Path]:
        if not roots or any(not root.is_dir() for root in roots):
            raise ValueError("allowed_roots must contain existing directories")
        return [root.resolve() for root in roots]
