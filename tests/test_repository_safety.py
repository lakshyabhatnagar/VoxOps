import json

import pytest
from conftest import copy_fixture, settings_for

from voxops.core.errors import AppError
from voxops.repository.filesystem_scanner import scan_repository
from voxops.repository.path_policy import workspace_path


# Why: Protect workspace containment against direct traversal and symbolic-link escapes.
# How: Attempt forbidden paths and verify the scanner skips links even inside allowed roots.
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


# Why: Ensure secret values and project side effects cannot escape a repository scan.
# How: Seed secrets, malformed files and executable source, then inspect serialized facts.
def test_scan_secrets_bounds_and_no_execution(tmp_path):
    root = copy_fixture(tmp_path, "fastapi_only")
    secret = "unique-sensitive-value-8675309"
    (root / ".env").write_text(f"DATABASE_URL={secret}\nTOKEN={secret}")
    (root / "package.json").write_text(json.dumps({"scripts": {"build": f"TOKEN={secret} vite"}}))
    (root / "large.py").write_text("x" * 150000)
    marker = tmp_path / "executed"
    (root / "poison.py").write_text(f"open({str(marker)!r}, 'w').write('executed')")
    scan = scan_repository(root, settings_for(tmp_path))
    serialized = scan.model_dump_json()
    assert secret not in serialized
    assert "DATABASE_URL" in serialized and "TOKEN" in serialized
    assert "fixture source was executed" not in serialized
    assert not marker.exists()
    assert scan.git.status == "uninitialized"
    assert any("large.py" in warning for warning in scan.warnings)
    bounded = scan_repository(root, settings_for(tmp_path, max_files=1))
    assert any("limit" in warning for warning in bounded.warnings)


# Why: Ensure unsupported or empty repositories produce diagnostics rather than failures.
# How: Scan an empty local directory and verify the typed scanner result remains valid.
def test_empty_repository(tmp_path):
    scan = scan_repository(tmp_path, settings_for(tmp_path))
    assert scan.manifests == []
    assert scan.git.status == "uninitialized"
