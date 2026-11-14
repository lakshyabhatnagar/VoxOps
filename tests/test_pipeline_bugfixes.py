import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import settings_for
from test_artifact_generation import CI, offline_agent
from test_artifact_planner import fixture_plan
from test_stage2_validation import plan_for

from voxops.artifacts.context_builder import build_context
from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.planner import build_plan
from voxops.artifacts.security import check_secret, redact, safe_read
from voxops.artifacts.validators import validate_artifact
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactTask
from voxops.execution.docker_driver import CommandOutcome
from voxops.execution.environment import EnvironmentManager
from voxops.execution.runner import ExecutionRunner
from voxops.repository.filesystem_scanner import source_evidence
from voxops.repository.snapshot import capture_source
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.mongodb import database_for


# Ordinary password validation and token processing contain no credential values.
# Check that they can be published and remain readable in model input.
# Literal credentials are checked separately by the existing secret-boundary tests.
@pytest.mark.parametrize(
    "source",
    [
        "const credentialsSchema = z.object({ password: z.string().min(8) });",
        "const token = jwt.decode(value);",
        "hashed_password = get_password_hash(user.password)",
        "const token = response.accessToken;",
        "token = decoded_token",
    ],
)
def test_credential_expressions_are_not_secrets(source):
    check_secret("auth.ts", source.encode())
    assert redact(source, path="auth.ts") == source


# A normal npm lockfile can exceed the old 128 KiB default.
# Read and hash it through both source capture and environment discovery.
# Also verify that a deliberately smaller configured limit still rejects it.
def test_large_lockfile_uses_consistent_limits(tmp_path):
    data = json.dumps({"padding": "x" * 187294}).encode()
    (tmp_path / "package-lock.json").write_bytes(data)
    assert safe_read(tmp_path, "package-lock.json") == data
    assert capture_source(tmp_path)[1]["package-lock.json"]
    run = SimpleNamespace(plan=SimpleNamespace(tasks={}))
    assert EnvironmentManager().source_digest(run, tmp_path)
    with pytest.raises(AppError, match="size limit"):
        EnvironmentManager(max_file_bytes=131072).source_digest(run, tmp_path)


# Test files belong in the required-file inventory without filling the prompt.
# Keep the startup module and dependency list available to the model.
# An explicitly requested test-file correction must still include that file's text.
def test_context_keeps_inventory_without_copying_every_test(tmp_path):
    root, analysis, _ = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    tests = root / "tests"
    tests.mkdir()
    paths = [f"tests/test_{index}.py" for index in range(20)]
    for path in paths:
        (root / path).write_text("# unnecessary prompt text\n" * 200)
    analysis.scan.relevant_files.extend(paths)
    plan = build_plan(root, analysis, {analysis.detected_units[0].id: "container"})
    task = next(t for t in plan.tasks.values() if t.artifact_type == "dockerfile")
    assert set(paths) <= set(task.required_paths)
    packet = build_context(root, task, plan, settings_for(tmp_path))
    assert not set(paths) & packet.files.keys()
    assert {"main.py", "requirements.txt"} <= packet.files.keys()
    assert set(paths) <= set(packet.requirements["task"]["required_paths"])
    patch = ArtifactTask(
        task_id="patch",
        artifact_type="source_patch",
        target_path=paths[0],
        reason="Correct this test",
    )
    assert paths[0] in build_context(root, patch, plan, settings_for(tmp_path)).files


# JavaScript permits quoted and unquoted object keys with the same meaning.
# Confirm that each spelling identifies the supported Next.js static export.
# This prevents a valid export from being mistaken for a server application.
@pytest.mark.parametrize("key", ["output", "'output'", '"output"'])
def test_quoted_next_export(key):
    facts = source_evidence("next.config.js", f"module.exports = {{ {key}: 'export' }}")
    assert any(fact.kind == "next_static_export" for fact in facts)


# Selecting a server strategy answers the ordinary SSR confirmation question.
# Keep genuinely conflicting static-export and server evidence unresolved.
# Exercise the unit questions and the duplicated analysis-level question together.
@pytest.mark.parametrize("strategy", ["native", "container"])
def test_explicit_next_server_strategy_confirms_ssr(tmp_path, strategy):
    root, analysis, _ = fixture_plan(tmp_path, "nextjs_fullstack", {"nextjs": strategy})
    unit = analysis.detected_units[0]
    question = "No static export: confirm SSR/runtime requirements."
    unit.detection.unresolved_questions = [question]
    unit.runtime.build_command, unit.runtime.start_command = "npm run build", "npm run start"
    analysis.unresolved_ambiguities = [f"{unit.root_path}: {question}"]
    plan = build_plan(root, analysis, {unit.id: strategy})
    assert not plan.unresolved_questions
    conflict = "Static export conflicts with server capability; review Next config."
    unit.detection.unresolved_questions = [conflict]
    analysis.unresolved_ambiguities = [f"{unit.root_path}: {conflict}"]
    assert build_plan(root, analysis, {unit.id: strategy}).unresolved_questions


# A forty-character hash is not enough to prove an action exists.
# Reject the exact invented setup-node hash seen in the real-repository test.
# Keep the independently verified hash acceptable without a network request.
def test_ci_rejects_invented_action_hash():
    task = ArtifactTask(
        task_id="ci", artifact_type="ci", target_path=".github/workflows/ci.yml", reason="Build"
    )
    valid = CI.replace(
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020",
    )
    assert validate_artifact(task, valid, plan_for(task))[0].status == "passed"
    invented = valid.replace("afbd3f7", "afbd6f7")
    assert validate_artifact(task, invented, plan_for(task))[0].status == "failed"


# Long install progress must not push the terminal failure out of the saved message.
# Preserve the final failure while keeping the diagnostic bounded.
# A credential at the end must be redacted before the output is shortened.
def test_failed_command_preserves_redacted_tail():
    outcome = CommandOutcome(
        exit_code=1,
        duration=0,
        output="progress\n" * 300 + "ACTUAL_FAILURE_AT_END\npassword = secretvalue123456",
    )
    with pytest.raises(AppError) as caught:
        ExecutionRunner._require_success(outcome, "build")
    assert "ACTUAL_FAILURE_AT_END" in caught.value.message
    assert "secretvalue123456" not in caught.value.message
    assert len(caught.value.message) < 1700


# A first request with no usage report should spend only its own allocation.
# Resume using persisted attempts and confirm a second request can finish.
# Check that Stage 2 uses its separate timeout and gives the model verified CI references.
def test_timeout_leaves_budget_for_resume(tmp_path, monkeypatch):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path, llm_call_retries=0)
    store = ArtifactStore(database_for(settings))
    packets, deadlines = [], []
    agent = offline_agent(lambda packet, content: packets.append(packet) or content)
    generator = ArtifactGenerator(settings, store, agent)
    task, identifier = plan.tasks["ci"], uuid4().hex
    original = asyncio.wait_for

    # Simulate the provider timeout without sleeping or making a real request.
    # The next call uses the normal offline model to complete the same file.
    # Closing the first coroutine avoids leaving a request running after timeout.
    async def timeout_once(coro, timeout):
        deadlines.append(timeout)
        if len(deadlines) == 1:
            coro.close()
            raise TimeoutError
        return await original(coro, timeout)

    monkeypatch.setattr(asyncio, "wait_for", timeout_once)
    result = asyncio.run(generator.generate(identifier, root, task, plan, ModelBudget(settings)))
    assert result.error_category == "model_timeout"
    assert result.attempts[0].reserved_tokens <= settings.artifact_task_tokens // 3
    result = asyncio.run(
        generator.generate(identifier, root, task, plan, ModelBudget(settings, result.attempts))
    )
    assert result.status == "passed"
    assert len(result.attempts) == 2
    assert deadlines == [180, 180]
    assert settings.model_timeout == 20
    assert "49933ea5288caeca8642d1e84afbd3f7d6820020" in json.dumps(packets[0])


# Keep real credential values blocked after allowing ordinary source expressions.
# Cover quoted source strings and unquoted configuration values containing dots.
# Redaction must hide the same values before diagnostics can be saved.
@pytest.mark.parametrize(
    "path,source",
    [
        ("settings.py", 'API_TOKEN = "real.literal.credential"'),
        ("auth.ts", 'const token = "real.literal.credential";'),
        ("config.yml", "password: real.literal.credential"),
        ("settings.ini", "API_TOKEN=real.literal.credential"),
    ],
)
def test_literal_credentials_still_blocked(path, source):
    with pytest.raises(AppError):
        check_secret(path, source.encode())
    assert "real.literal.credential" not in redact(source, path=path)


# Apply the larger source limit to environment discovery as well as source hashing.
# Put an ordinary environment read after enough comments to exceed the old analyzer limit.
# Verify that the field is discovered without guessing about unusual syntax.
def test_environment_discovery_respects_file_limit(tmp_path):
    from test_environment_analysis import unit

    from voxops.execution.environment_analysis import Analyzer

    (tmp_path / "main.py").write_text(
        "# harmless padding\n" * 16000 + "import os\nPORT = os.getenv('PORT', '8000')\n"
    )
    schema, _ = Analyzer(tmp_path, [unit()], "0" * 64).scan()
    assert not schema.ambiguities
    assert any(field.name == "PORT" for field in schema.requirements)


# Expose the approved defaults and upper bounds through the actual settings parser.
# Keep the optional analysis timeout separate from file generation and repair.
# Oversized configurations must still be rejected rather than becoming unbounded.
def test_approved_limit_settings(tmp_path):
    from pydantic import ValidationError

    settings = settings_for(tmp_path)
    assert settings.max_file_bytes == 1048576
    assert settings.max_context_chars == 64000
    assert settings.artifact_model_timeout == 180
    assert settings.model_timeout == 20
    settings_for(tmp_path, max_context_chars=128000, artifact_model_timeout=300)
    with pytest.raises(ValidationError):
        settings_for(tmp_path, max_context_chars=128001)
    with pytest.raises(ValidationError):
        settings_for(tmp_path, artifact_model_timeout=301)
