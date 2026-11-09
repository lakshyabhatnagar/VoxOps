import asyncio
import json
import os

import pytest
from conftest import settings_for
from test_artifact_planner import fixture_plan
from test_environment_analysis import unit
from test_environment_session import schema

from voxops.artifacts.context_builder import build_context
from voxops.artifacts.security import check_secret, redact
from voxops.core.errors import AppError
from voxops.execution.docker_driver import DockerDriver
from voxops.execution.environment import SecretSession
from voxops.execution.environment_analysis import Analyzer

PRIVATE = "synthetic-private-default"
PRIVATE_SOURCES = [
    f'API_TOKEN: str = "{PRIVATE}"',
    f'API_TOKEN: str = "{PRIVATE}"\ndef broken(:\n',
    f'from pydantic import Field\nAPI_TOKEN: str = Field(default="{PRIVATE}")',
    f'from pydantic import Field as F\nAPI_TOKEN: str = F("{PRIVATE}")',
    f'import os\nvalue = os.getenv("API_TOKEN", "{PRIVATE}")',
    f'import os as operating\nvalue = operating.environ.get("API_TOKEN", "{PRIVATE}")',
    f'from os import getenv as get\nvalue = get("API_TOKEN", "{PRIVATE}")',
    f'const value = process.env.API_TOKEN ?? "{PRIVATE}";',
    f'const value = process.env["API_TOKEN"] ?? "{PRIVATE}";',
    f"const value = import.meta.env['API_TOKEN'] || '{PRIVATE}';",
    f"const value = process.env.API_TOKEN ?? `{PRIVATE}`;",
    f'const value = import.meta.env.API_TOKEN || "{PRIVATE}";',
    f'const API_TOKEN: string = "{PRIVATE}";',
    "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
    "class Settings(BaseSettings):\n"
    '    model_config = SettingsConfigDict(env_prefix="SECRET_")\n'
    f'    value: str = "{PRIVATE}"\n',
    "from pydantic_settings import BaseSettings as BS, SettingsConfigDict as Config\n"
    "class Parent(BS):\n"
    f'    value: str = "{PRIVATE}"\n'
    "class Settings(Parent):\n"
    '    model_config = Config(env_prefix="SECRET_")\n',
    "from pydantic_settings import BaseSettings\nfrom pydantic import Field as F\n"
    "class Settings(BaseSettings):\n"
    f'    value: str = F(default="{PRIVATE}", validation_alias="API_TOKEN")\n',
    f'import os\nvalue = os.getenv("API_TOKEN", default="{PRIVATE}")',
    f'import os\nvalue = os.environ.get("API_TOKEN", default="{PRIVATE}")',
    "from pydantic_settings import BaseSettings\nfrom pydantic import Field\n"
    "class Settings(BaseSettings):\n"
    f'    value: str = Field("{PRIVATE}", alias="VITE_VALUE", validation_alias="API_TOKEN")',
]


@pytest.mark.parametrize("source", PRIVATE_SOURCES)
def test_private_source_defaults_do_not_reach_context_or_publication(tmp_path, source):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "main.py").write_text(source)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    task.reason = source
    packet = build_context(root, task, plan, settings_for(tmp_path))
    assert PRIVATE not in packet.model_dump_json()
    assert "main.py" not in packet.files
    assert packet.warnings
    assert PRIVATE not in redact(source)
    with pytest.raises(AppError, match="Probable credential"):
        check_secret("main.py", source.encode(), allow_binary=True)


@pytest.mark.parametrize(
    "source",
    [
        'import os\nTOKEN = os.environ["TOKEN"]',
        'const key = import.meta.env.VITE_API_KEY ?? "browser-publishable-key";',
        'const key = process.env.NEXT_PUBLIC_API_KEY || "browser-publishable-key";',
        'const VITE_API_KEY: string = "browser-publishable-key";',
        'VITE_API_KEY = "browser-publishable-key"',
        'PORT: int = 8000\nHOST: str = "localhost"\nAPI_TOKEN: str | None = None',
        'from pydantic import Field\nAPI_TOKEN: str = Field(description="API token metadata")',
        'DATABASE_URL: str = "postgresql://localhost/app"',
        'const mode = import.meta.env.MODE ?? "production";',
        "from pydantic import Field\nAPI_TOKEN: str | None = Field(default=None)",
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
        "class Parent(BaseSettings):\n"
        '    API_KEY: str = "browser-publishable-key"\n'
        "class Settings(Parent):\n"
        '    model_config = SettingsConfigDict(env_prefix="VITE_")\n',
        "from pydantic_settings import BaseSettings\nfrom pydantic import Field\n"
        "class Settings(BaseSettings):\n"
        '    API_KEY: str = Field("browser-publishable-key", validation_alias="VITE_API_KEY")',
        "from pydantic_settings import BaseSettings\nfrom pydantic import Field\n"
        "class Settings(BaseSettings):\n"
        '    value: str = Field("browser-publishable-key", '
        'alias="API_TOKEN", validation_alias="VITE_VALUE")',
    ],
)
def test_public_and_noncredential_source_remains_readable(tmp_path, source):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "main.py").write_text(source)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    packet = build_context(root, task, plan, settings_for(tmp_path))
    assert packet.files["main.py"] == source
    assert redact(source) == source
    check_secret("main.py", source.encode())


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 30,
        "sk-" + "a" * 30,
        "AKIA" + "A" * 16,
        "postgresql://user:private-password@example.com/db",
    ],
)
@pytest.mark.parametrize(
    "source_pattern",
    [
        'const key = import.meta.env.VITE_API_KEY ?? "{}";',
        'VITE_API_KEY = "{}"',
        'NEXT_PUBLIC_API_KEY = "{}"',
    ],
)
def test_public_name_does_not_hide_known_credential_format(secret, source_pattern):
    source = source_pattern.format(secret)
    with pytest.raises(AppError):
        check_secret("config.ts", source.encode())
    assert secret not in redact(source)


def test_context_does_not_rescan_environment_or_read_env_values(tmp_path, monkeypatch):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / ".env").write_text(f"API_TOKEN={PRIVATE}\n")

    def unexpected_scan(self):
        raise AssertionError("Prompt construction must not resolve environment values")

    monkeypatch.setattr(Analyzer, "scan", unexpected_scan)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    packet = build_context(root, task, plan, settings_for(tmp_path))
    assert "main.py" in packet.files
    assert ".env" not in packet.files
    assert PRIVATE not in packet.model_dump_json()


def test_static_prefix_matches_environment_visibility(tmp_path):
    source = next(
        source for source in PRIVATE_SOURCES if 'SettingsConfigDict(env_prefix="SECRET_")' in source
    )
    (tmp_path / "settings.py").write_text(source)
    form, defaults = Analyzer(tmp_path, [unit()], "a" * 64).scan()
    assert form.requirements[0].name == "SECRET_value"
    assert form.requirements[0].visibility == "secret"
    assert PRIVATE in defaults.values()
    with pytest.raises(AppError):
        check_secret("settings.py", source.encode())


def test_session_redacts_json_and_retains_rotated_secrets():
    old = 'old-quote"-backslash\\-private'
    new = 'new-quote"-backslash\\-private'
    session = SecretSession(schema(), {"secret": old})
    session.submit({"secret": new})
    for value in (old, new):
        assert value not in session.redact(value)
        assert json.dumps(value)[1:-1] not in session.redact(json.dumps({"detail": value}))
    assert "VITE_API_URL" in schema().model_dump_json()
    with pytest.raises(AppError):
        session.submit({"public": f"https://example.com/{new}"})


def test_multiline_default_is_redacted_from_docker_line_drain(tmp_path, monkeypatch):
    value = 'first-private-line\nsecond-private-"line\\tail'
    (tmp_path / "settings.py").write_text(
        "from pydantic_settings import BaseSettings\nclass Settings(BaseSettings):\n"
        f"    API_TOKEN: str = {value!r}\n"
    )
    form, defaults = Analyzer(tmp_path, [unit()], "a" * 64).scan()
    session = SecretSession(form, defaults=defaults)
    executable = tmp_path / "docker"
    output = value + "\n" + json.dumps({"detail": value}) + "\ncomplete\n"
    executable.write_text(f"#!/usr/bin/python3\nimport sys\nsys.stdout.write({output!r})\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    async def run():
        driver = DockerDriver(
            settings_for(tmp_path),
            "run",
            "owner",
            lambda item: None,
            asyncio.Event(),
            session.redact,
        )
        return await driver.command(["docker", "logs", "fake"])

    outcome = asyncio.run(run())
    assert outcome.exit_code == 0
    assert "complete" in outcome.output
    for line in value.splitlines():
        assert line not in outcome.output
        assert json.dumps(line)[1:-1] not in outcome.output
