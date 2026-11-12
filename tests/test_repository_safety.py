import json

import pytest
from conftest import copy_fixture, settings_for

from voxops.core.errors import AppError
from voxops.repository.filesystem_scanner import scan_repository
from voxops.repository.path_policy import workspace_path


# Try paths outside the allowed folder and add symbolic links to a sample.
# Check that access is refused or the links are skipped.
def test_path_and_symlink_policy(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "package.json").write_text('{"dependencies":{"next":"1"}}')
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    for value in [str(allowed / ".." / "outside"), str(allowed / "escape"), str(outside)]:
        with pytest.raises(AppError):
            workspace_path(value, [allowed])
    assert not scan_repository(allowed, settings_for(tmp_path)).manifests


# Add secrets, malformed files, and executable code to a sample repository.
# Check that scanning hides the secrets and never runs the code.
def test_scan_secrets_bounds_and_no_execution(tmp_path):
    root = copy_fixture(tmp_path, "fastapi_only")
    secret = "unique-sensitive-value-8675309"
    (root / ".env").write_text(f"DATABASE_URL={secret}\nTOKEN={secret}")
    (root / "package.json").write_text(json.dumps({"scripts": {"build": f"TOKEN={secret} vite"}}))
    (root / "large.py").write_text("x" * 150000)
    marker = tmp_path / "executed"
    (root / "poison.py").write_text(f"open({str(marker)!r}, 'w').write('executed')")
    scan = scan_repository(root, settings_for(tmp_path, max_file_bytes=131072))
    serialized = scan.model_dump_json()
    assert secret not in serialized
    assert "DATABASE_URL" in serialized and "TOKEN" in serialized
    assert "fixture source was executed" not in serialized
    assert not marker.exists()
    assert scan.git.status == "uninitialized"
    assert any("large.py" in warning for warning in scan.warnings)
    bounded = scan_repository(root, settings_for(tmp_path, max_files=1))
    assert any("limit" in warning for warning in bounded.warnings)


# Scan an empty temporary directory.
# Check that the result contains no manifests and reports the empty repository.
def test_empty_repository(tmp_path):
    scan = scan_repository(tmp_path, settings_for(tmp_path))
    assert scan.manifests == []
    assert scan.git.status == "uninitialized"


def test_env_fingerprint_uses_names_without_hashing_secret_values(tmp_path):
    root = copy_fixture(tmp_path, "fastapi_only")
    settings = settings_for(tmp_path)
    env = root / ".env"
    env.write_text("DATABASE_URL=first-private-value\n")
    first = scan_repository(root, settings)
    env.write_text("DATABASE_URL=different-private-value\n")
    changed_value = scan_repository(root, settings)
    assert first.input_fingerprint == changed_value.input_fingerprint

    env.write_text("DATABASE_URL=different-private-value\nNEW_REQUIRED_NAME=\n")
    changed_names = scan_repository(root, settings)
    assert first.input_fingerprint != changed_names.input_fingerprint
