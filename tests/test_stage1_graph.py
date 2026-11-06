import asyncio
import json

from conftest import copy_fixture, settings_for
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from voxops.agents.topology_agent import INSTRUCTIONS, TopologyOutput
from voxops.analysis.topology_resolver import TopologyResolver
from voxops.core.errors import AppError
from voxops.frameworks.registry import detect_units
from voxops.repository.filesystem_scanner import scan_repository
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


# Analyze a sample repository without a model API key.
# Check that the result is saved and matches the returned analysis.
def test_graph_no_key_and_persistence(tmp_path):
    root = copy_fixture(tmp_path, "nextjs_fullstack")
    settings = settings_for(tmp_path)
    store = ScanStore(settings.data_dir)
    workflow = Stage1Workflow(settings, store)
    result = asyncio.run(workflow.analyze(str(root), use_llm=True))
    assert result.deterministic_only and result.ai_usage.status == "unavailable"
    assert result.detected_units[0].role == "fullstack_web"
    assert any("credential" in warning for warning in result.warnings)
    saved = json.loads((settings.data_dir / f"scan-{result.scan.id}.json").read_text())
    assert saved == result.model_dump(mode="json")
    assert "path" not in saved["scan"]


# Analyze a Vite sample with a controlled model response.
# Check that the result retains facts established by the scan.
def test_mocked_structured_topology_and_precedence(tmp_path):
    root = copy_fixture(tmp_path, "vite_only")
    (root / "vite.config.ts").write_text("export default { build: { ssr: 'src/entry.ts' } }")
    settings = settings_for(tmp_path)
    unit = detect_units(scan_repository(root, settings))[0]
    output = {
        "assessments": [
            {
                "unit_id": unit.id,
                "role": "server_rendered_web",
                "confidence": 0.8,
                "conclusion": "supported",
            }
        ]
    }
    model = TestModel(custom_output_args=output)
    agent = Agent(model, output_type=TopologyOutput, instructions=INSTRUCTIONS)
    resolver = TopologyResolver(settings, agent)
    workflow = Stage1Workflow(settings, ScanStore(settings.data_dir), resolver)
    result = asyncio.run(workflow.analyze(str(root)))
    assert not result.deterministic_only
    assert result.ai_usage.requests == 1
    assert result.detected_units[0].role == "server_rendered_web"
    assert result.detected_units[0].detection.evidence[-1].source == "llm"
    assert len(resolver.context(result.detected_units)) <= settings.max_context_chars
    # Give the model a response that suggests static hosting.
    # Check that the detected server and API features are kept.
    next_root = copy_fixture(tmp_path, "nextjs_fullstack")
    next_unit = detect_units(scan_repository(next_root, settings))[0]
    model.custom_output_args = {
        "assessments": [
            {
                "unit_id": next_unit.id,
                "role": "static_frontend",
                "confidence": 1,
                "conclusion": "supported",
            }
        ]
    }
    result = asyncio.run(workflow.analyze(str(next_root), use_llm=True))
    assert result.detected_units[0].role == "fullstack_web"
    assert any("contradicted" in warning for warning in result.warnings)


# Trigger invalid model output, a policy error, and a storage failure.
# Check that the analysis reports each failure appropriately.
def test_graph_failure_transitions(tmp_path):
    root = copy_fixture(tmp_path, "vite_only")
    settings = settings_for(tmp_path)
    agent = Agent(
        TestModel(custom_output_args={"unexpected": "secret-sentinel"}),
        output_type=TopologyOutput,
        retries=0,
    )
    workflow = Stage1Workflow(
        settings, ScanStore(settings.data_dir), TopologyResolver(settings, agent)
    )
    result = asyncio.run(workflow.analyze(str(root), use_llm=True))
    assert result.deterministic_only and result.ai_usage.status == "failed"
    assert "secret-sentinel" not in result.model_dump_json()
    try:
        asyncio.run(workflow.analyze(str(tmp_path / "..")))
    except AppError as error:
        assert error.category == "path_traversal"
    else:
        raise AssertionError("Traversal was accepted")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    workflow = Stage1Workflow(settings, ScanStore(blocked))
    try:
        asyncio.run(workflow.analyze(str(root)))
    except AppError as error:
        assert error.category == "storage_unavailable"
    else:
        raise AssertionError("Storage failure was ignored")
