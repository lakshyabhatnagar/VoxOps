import os
import pickle

import pytest

from voxops.core.errors import AppError
from voxops.domain.execution_models import EnvConsumer, EnvFormSchema, EnvRequirement
from voxops.execution.environment import SecretSession, read_env_file


def schema():
    return EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="secret",
                name="TOKEN",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="required",
                visibility="secret",
            ),
            EnvRequirement(
                field_id="public",
                name="VITE_API_URL",
                consumers=[EnvConsumer(unit_id="web", phase="build")],
                necessity="optional",
                visibility="public",
                value_type="url",
            ),
        ],
    )


def test_submission_is_atomic_and_secret_is_private(tmp_path):
    session = SecretSession(schema())
    with pytest.raises(AppError):
        session.submit({"secret": "secret-value", "public": "bad"})
    assert session.values_for("api", "runtime", "secret") == {}
    session.submit({"secret": "secret-value", "public": "https://example.com"})
    assert "secret-value" not in repr(session)
    assert "secret-value" not in session.redact("token=secret-value")
    with pytest.raises(TypeError):
        pickle.dumps(session)
    with session.runtime_file("api") as path:
        assert path.read_text() == "TOKEN=secret-value\n"
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert not path.exists()


def test_env_file_is_bounded_and_never_follows_symlinks(tmp_path):
    (tmp_path / ".env").write_text("TOKEN='private' # comment\nHOST=localhost # hostname\n")
    values, version = read_env_file(tmp_path, ".env")
    assert values == {"TOKEN": "private", "HOST": "localhost"}
    assert version is not None
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").symlink_to(tmp_path / "missing")
    with pytest.raises(AppError):
        read_env_file(tmp_path, ".env")
    with pytest.raises(AppError):
        read_env_file(tmp_path, "../.env")


def test_env_file_rejects_unresolved_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_HEAD", "host-only-private-value")
    (tmp_path / ".env").write_text("TOKEN_HEAD=local\nTOKEN=${TOKEN_HEAD}-credential\n")
    with pytest.raises(AppError, match="safely read"):
        read_env_file(tmp_path, ".env")


def test_public_url_can_reuse_known_port_default_but_not_private_value():
    form = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="port",
                name="PORT",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="optional",
                visibility="unknown",
                value_type="port",
                default_kind="literal",
                resolution="default",
            ),
            EnvRequirement(
                field_id="key",
                name="SECRET_KEY",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="optional",
                visibility="secret",
                default_kind="literal",
                resolution="default",
            ),
            EnvRequirement(
                field_id="url",
                name="VITE_API_URL",
                consumers=[EnvConsumer(unit_id="web", phase="build")],
                necessity="optional",
                visibility="public",
                value_type="url",
            ),
        ],
    )
    session = SecretSession(form, defaults={"port": "8000", "key": "private-value"})
    session.submit({"url": "http://localhost:8000"})
    assert session.redact("port 8000") == "port 8000"
    with pytest.raises(AppError):
        session.submit({"url": "https://private-value.example"})


@pytest.mark.parametrize("value", [" ", "xxx", "example", "YOUR_KEY", "<insert_here>"])
def test_required_secret_placeholders_are_rejected(value):
    with pytest.raises(AppError):
        SecretSession(schema()).submit({"secret": value})


def test_general_service_urls_and_typed_values():
    form = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="db",
                name="DATABASE_URL",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="required",
                value_type="url",
            ),
            EnvRequirement(
                field_id="workers",
                name="WORKERS",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="required",
                value_type="integer",
            ),
            EnvRequirement(
                field_id="enabled",
                name="ENABLED",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
                necessity="required",
                value_type="boolean",
            ),
        ],
    )
    session = SecretSession(form)
    with pytest.raises(AppError):
        session.submit({"db": "mongodb+srv://example.org/db", "workers": "many", "enabled": "true"})
    session.submit({"db": "mongodb+srv://example.org/db", "workers": "3", "enabled": "true"})
