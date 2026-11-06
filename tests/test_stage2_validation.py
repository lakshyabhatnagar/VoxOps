from pathlib import Path

import pytest

from voxops.artifacts.context_builder import build_context
from voxops.artifacts.patcher import repair_gitignore
from voxops.artifacts.security import check_secret, redact, safe_read
from voxops.artifacts.validators import validate_artifact, validate_cross_artifacts
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactPlan, ArtifactTask
from voxops.domain.models import DeployableUnit, RuntimeSpec


# Build a small file plan from the supplied tasks.
# Return it for validation without scanning a repository.
def plan_for(*tasks: ArtifactTask) -> ArtifactPlan:
    return ArtifactPlan(
        plan_id="p",
        scan_id="s",
        repo_fingerprint="r",
        source_input_fingerprint="f",
        strategy_by_unit={},
        units=[],
        tasks={task.task_id: task for task in tasks},
    )


# Create sensitive files and symbolic links in a temporary repository.
# Check that file reads and secret checks reject unsafe access.
def test_security_boundaries(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SENTINEL")
    (tmp_path / ".npmrc").write_text("SENTINEL")
    (tmp_path / "secret.pem").write_text("SENTINEL")
    outside = tmp_path.parent / "outside-voxops-test"
    outside.mkdir(exist_ok=True)
    (outside / "package.json").write_text("{}")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    for path in (".env", ".npmrc", "secret.pem", "linked/package.json"):
        with pytest.raises(AppError):
            safe_read(tmp_path, path)
    assert safe_read(tmp_path, "missing") is None
    sentinel = "ghp_" + "a" * 30
    with pytest.raises(AppError) as error:
        check_secret("config.json", ("token=" + sentinel).encode())
    assert sentinel not in str(error.value)
    assert sentinel not in redact("token=" + sentinel)


@pytest.mark.parametrize(
    "value",
    [
        'DATABASE_URL = "postgresql://admin:supers3cret@database.invalid/app"',
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
# Supply credentials in URLs and headers.
# Check that secret detection rejects them without exposing their values.
def test_url_and_header_credentials_are_rejected(value):
    with pytest.raises(AppError):
        check_secret("settings.py", value.encode())
    assert "supers3cret" not in redact(value)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in redact(value)


# Put required Docker instructions only in an earlier build stage.
# Check that they do not satisfy the checks for the final stage.
def test_docker_final_stage_policy():
    task = ArtifactTask(
        task_id="d",
        artifact_type="dockerfile",
        target_path="Dockerfile",
        reason="final runtime policy",
    )
    content = "FROM python:3.12 AS builder\nUSER 1000\nCMD python main.py\nFROM python:3.12\n"
    findings = validate_artifact(task, content, plan_for(task))
    assert findings[0].status == "failed"
    assert any("non-root" in message for message in findings[0].messages)
    assert any("runtime CMD" in message for message in findings[0].messages)


# Add a wildcard origin to a multiline list of allowed origins.
# Check that validation rejects the unsafe change.
def test_multiline_cors_patch_is_rejected(tmp_path):
    (tmp_path / "app.py").write_text('allow_origins=[\n    "existing"\n]\n')
    task = ArtifactTask(
        task_id="p", artifact_type="source_patch", target_path="app.py", reason="CORS policy"
    )
    result = validate_artifact(task, 'allow_origins=[\n    "*"\n]\n', plan_for(task), root=tmp_path)
    assert result[0].status == "failed"


@pytest.mark.parametrize("key", ["client_secret", "DB_PASSWORD", "openaiApiKey", "access_token"])
# Put a recognizable secret under several credential names.
# Check that publication rejects it and model input leaves it out.
def test_compound_secret_names_are_rejected(tmp_path, key):
    sentinel = "GOCSPX_STAGE2_SECRET_SENTINEL_123456789"
    value = f'{{"{key}": "{sentinel}"}}'
    with pytest.raises(AppError, match="Probable credential"):
        check_secret("config.json", value.encode())
    assert sentinel not in redact(value)
    (tmp_path / "config.json").write_text(value)
    task = ArtifactTask(
        task_id="t",
        artifact_type="ci",
        target_path="ci.yml",
        required_paths=["config.json"],
        reason="credential regression",
    )
    packet = build_context(tmp_path, task, plan_for(task), Settings(allowed_roots=[tmp_path]))
    assert sentinel not in packet.model_dump_json() and "config.json" not in packet.files


@pytest.mark.parametrize("field", ["runner", "action", "run", "permission"])
# Put lists where a workflow expects simple values.
# Check that validation reports errors instead of crashing.
def test_workflow_yaml_types_return_validation(field):
    task = ArtifactTask(task_id="c", artifact_type="ci", target_path="ci.yml", reason="schema")
    values = {
        "runner": "[ubuntu-latest]" if field == "runner" else "ubuntu-latest",
        "action": "[invalid]" if field == "action" else "actions/checkout@" + "a" * 40,
        "run": "[echo, hello]" if field == "run" else "echo hello",
        "permission": "[read]" if field == "permission" else "read",
    }
    content = (
        f"on: push\npermissions: {{contents: {values['permission']}}}\njobs:\n"
        f"  build:\n    runs-on: {values['runner']}\n    steps:\n"
        f"      - uses: {values['action']}\n      - run: {values['run']}\n"
    )
    assert validate_artifact(task, content, plan_for(task))[0].status == "failed"


# Build model input from files containing a secret and untrusted text.
# Check that the secret is omitted and the untrusted text is marked.
def test_context_omits_secret_and_marks_untrusted(tmp_path: Path) -> None:
    sentinel = "ghp_" + "b" * 30
    (tmp_path / "config.json").write_text("token=" + sentinel)
    (tmp_path / "package.json").write_text('{"scripts":{"build":"npm run build"}}')
    task = ArtifactTask(
        task_id="t",
        artifact_type="ci",
        target_path=".github/workflows/ci.yml",
        required_paths=["config.json", "package.json"],
        reason="CI",
    )
    packet = build_context(tmp_path, task, plan_for(task), Settings(allowed_roots=[tmp_path]))
    assert "package.json" in packet.files
    assert "config.json" not in packet.files
    assert sentinel not in packet.model_dump_json()
    assert "untrusted data" in packet.requirements["instruction"]
    assert packet.source_fingerprint == "f"


# Validate an incomplete ignore file and add the missing protected patterns.
# Check that the repaired file passes while keeping its existing rules.
def test_ignore_repair_preserves_existing_and_validates() -> None:
    task = ArtifactTask(
        task_id="i",
        artifact_type="gitignore",
        target_path=".gitignore",
        required_paths=["package.json"],
        reason="protect credentials",
    )
    original = "# existing\nnode_modules/\n"
    assert validate_artifact(task, original, plan_for(task))[0].status == "failed"
    repaired = repair_gitignore(original)
    assert repaired.startswith(original)
    assert repair_gitignore(repaired) == repaired
    assert validate_artifact(task, repaired, plan_for(task))[0].status == "passed"


# Validate a broken Dockerfile and a valid non-root Dockerfile.
# Check that the broken file fails and the valid one passes.
def test_docker_validation(tmp_path: Path) -> None:
    task = ArtifactTask(
        task_id="d", artifact_type="dockerfile", target_path="Dockerfile", reason="container"
    )
    plan = plan_for(task)
    assert (
        validate_artifact(task, "RUN echo FROM python\n", plan, root=tmp_path)[0].status == "failed"
    )
    valid = 'FROM python:3.12\nWORKDIR /app\nCOPY . .\nUSER 1000\nCMD ["python", "main.py"]\n'
    assert validate_artifact(task, valid, plan, root=tmp_path)[0].status == "passed"


# Validate workflow commands and permissions for safe and unsafe examples.
# Check that unsafe input and excess write access are rejected.
def test_workflow_security() -> None:
    task = ArtifactTask(
        task_id="c",
        artifact_type="ci",
        target_path=".github/workflows/ci.yml",
        expected_commands=["npm test"],
        reason="test",
    )
    plan = plan_for(task)
    valid = (
        "on: [push, pull_request]\npermissions:\n  contents: read\njobs:\n  test:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "a" * 40
        + "\n        with:\n          persist-credentials: false\n      - run: npm test\n"
    )
    assert validate_artifact(task, valid, plan)[0].status == "passed"
    assert (
        validate_artifact(
            task, valid.replace("persist-credentials: false", "persist-credentials: true"), plan
        )[0].status
        == "failed"
    )
    token_env = valid + "env:\n  EXPOSED: ${{ github.token }}\n"
    assert validate_artifact(task, token_env, plan)[0].status == "failed"
    assert (
        validate_artifact(
            task, valid + "      - run: echo ${{ github.event.issue.title }}\n", plan
        )[0].status
        == "failed"
    )


# Pair a Dockerfile that copies its build context with a weak ignore file.
# Check that validation warns about files that could leak into the image.
def test_cross_artifact_context_leak() -> None:
    docker = ArtifactTask(
        task_id="d", artifact_type="dockerfile", target_path="Dockerfile", reason="container"
    )
    ignore = ArtifactTask(
        task_id="i", artifact_type="dockerignore", target_path=".dockerignore", reason="context"
    )
    result = validate_cross_artifacts(
        plan_for(docker, ignore), {"Dockerfile": "FROM python\nCOPY . .\n", ".dockerignore": ""}
    )
    assert result[0].status == "failed"


# Request a symbolic link and then input that exceeds the size limit.
# Check that neither is silently accepted as empty input.
def test_context_blocks_unsafe_and_oversize(tmp_path: Path) -> None:
    task = ArtifactTask(
        task_id="t",
        artifact_type="ci",
        target_path="ci.yml",
        required_paths=["package.json"],
        reason="CI",
    )
    (tmp_path / "package.json").symlink_to("missing")
    with pytest.raises(AppError) as error:
        build_context(tmp_path, task, plan_for(task), Settings(allowed_roots=[tmp_path]))
    assert error.value.category == "unsafe_path"
    (tmp_path / "package.json").unlink()
    (tmp_path / "package.json").write_text("x" * 1100)
    with pytest.raises(AppError) as error:
        build_context(
            tmp_path,
            task,
            plan_for(task),
            Settings(allowed_roots=[tmp_path], max_context_chars=1000),
        )
    assert error.value.category == "context_too_large"


# Validate a broken Python edit and a reusable workflow job.
# Check that both unsafe outputs are rejected.
def test_invalid_patch_and_reusable_job(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    patch = ArtifactTask(
        task_id="p", artifact_type="source_patch", target_path="app.py", reason="patch"
    )
    assert (
        validate_artifact(patch, "def broken(:\n", plan_for(patch), root=tmp_path)[0].status
        == "failed"
    )
    ci = ArtifactTask(task_id="c", artifact_type="ci", target_path="ci.yml", reason="CI")
    workflow = (
        "on: push\npermissions: {contents: read}\njobs:\n"
        "  reuse:\n    uses: owner/repo/x.yml@main\n"
    )
    assert validate_artifact(ci, workflow, plan_for(ci))[0].status == "failed"


# Compare Docker startup commands with the discovered app command.
# Check that incorrect commands fail and equivalent commands pass.
def test_docker_runtime_matches_evidence() -> None:
    task = ArtifactTask(
        task_id="d",
        unit_id="api",
        artifact_type="dockerfile",
        target_path="Dockerfile",
        reason="container",
    )
    runtime = RuntimeSpec(language="python", start_command="uvicorn app:app --host 0.0.0.0")
    unit = DeployableUnit.model_construct(id="api", runtime=runtime)
    plan = plan_for(task).model_copy(update={"units": [unit]})
    prefix = "FROM python:3.12\nUSER 1000\n"
    wrong = prefix + 'CMD ["python", "other.py"]\n'
    valid = prefix + 'CMD ["uvicorn", "app:app", "--host", "0.0.0.0"]\n'
    assert validate_artifact(task, wrong, plan)[0].status == "failed"
    assert validate_artifact(task, valid, plan)[0].status == "passed"


# Change a JSON configuration file to invalid JSON.
# Check that validation rejects the edit before review.
def test_json_patch_syntax(tmp_path):
    (tmp_path / "config.json").write_text('{"port": 8000}\n')
    task = ArtifactTask(
        task_id="p",
        artifact_type="source_patch",
        target_path="config.json",
        reason="configuration repair",
    )
    result = validate_artifact(task, '{"port": 8080', plan_for(task), root=tmp_path)
    assert result[0].status == "failed"
    assert "JSON source patch has invalid syntax." in result[0].messages


@pytest.mark.parametrize("binding", ["VITE_PRIVATE_TOKEN", "NEXT_PUBLIC_API_KEY"])
# Change browser-facing code to reference a server secret.
# Check that validation blocks the edit even without a literal secret value.
def test_source_patch_cannot_expose_secret_binding(tmp_path, binding):
    (tmp_path / "client.js").write_text('const api = "configured";\n')
    task = ArtifactTask(
        task_id="p",
        artifact_type="source_patch",
        target_path="client.js",
        reason="frontend configuration",
    )
    proposal = f"const api = import.meta.env.{binding};\n"
    result = validate_artifact(task, proposal, plan_for(task), root=tmp_path)
    assert result[0].status == "failed"
    assert any("browser-public configuration" in message for message in result[0].messages)
