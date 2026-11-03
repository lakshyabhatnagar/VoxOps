from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Why: Centralize bounded local execution and optional model configuration.
# How: Validate environment settings without requiring any cloud credential.
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
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    @field_validator("allowed_roots")
    # Why: Prevent an empty policy from accidentally authorizing every filesystem path.
    # How: Require at least one existing canonical workspace directory.
    def valid_roots(cls, roots: list[Path]) -> list[Path]:
        if not roots or any(not root.is_dir() for root in roots):
            raise ValueError("allowed_roots must contain existing directories")
        return [root.resolve() for root in roots]
