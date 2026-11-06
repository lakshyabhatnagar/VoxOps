from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Read application paths, limits, and service choices from environment variables.
# Keep each provider's optional key separate and hide key values when settings are printed.
# Check local paths and limits even when no external service has been configured.
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOXOPS_", extra="ignore")
    allowed_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    data_dir: Path = Path(".voxops")
    max_depth: int = Field(default=8, ge=1, le=20)
    max_files: int = Field(default=3000, ge=1, le=20000)
    max_file_bytes: int = Field(default=131072, ge=1024, le=1048576)
    max_context_chars: int = Field(default=16000, ge=1000, le=32000)
    model_timeout: float = Field(default=20, ge=1, le=60)
    model: str = "anthropic:claude-sonnet-4-5"
    artifact_concurrency: int = Field(default=3, ge=1, le=8)
    artifact_max_requests: int = Field(default=60, ge=1, le=300)
    artifact_max_tokens: int = Field(default=150000, ge=1000, le=1000000)
    artifact_task_tokens: int = Field(default=20000, ge=1000, le=100000)
    artifact_staged_bytes: int = Field(default=8388608, ge=131072, le=67108864)
    artifact_optional_tools: bool = False
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
