from types import SimpleNamespace

import pytest

from voxops.domain.execution_models import EnvFormSchema
from voxops.domain.models import ConfigRequirement, DeployableUnit, FrameworkDetection, RuntimeSpec
from voxops.execution.environment import EnvironmentManager, SecretSession
from voxops.execution.environment_analysis import Analyzer


def unit(root=".", *, javascript=False):
    framework = "react_vite" if javascript else "fastapi"
    role = "static_frontend" if javascript else "http_service"
    language = "javascript" if javascript else "python"
    return DeployableUnit(
        id=root,
        name=root,
        root_path=root,
        role=role,
        framework=framework,
        language=language,
        runtime=RuntimeSpec(language=language),
        detection=FrameworkDetection(
            framework=framework, root_path=root, role_candidate=role, evidence=[], confidence=1
        ),
    )


def test_python_inheritance_defaults_and_optional_reads_do_not_leak(tmp_path):
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings as BS, SettingsConfigDict as Config
from pydantic import Field as F
import os
class Parent(BS):
    model_config = Config(env_prefix='APP_')
    TOKEN: str
    OPTIONAL: str
class Settings(Parent):
    OPTIONAL: str | None = None
    api_key: str = F(validation_alias='EXACT_KEY')
    PRIVATE: str = 'private-default-value'
def feature():
    return os.environ['FEATURE_ONLY']
""")
    schema, defaults = Analyzer(tmp_path, [unit()], "a" * 64).scan()
    fields = {item.name: item for item in schema.requirements}
    assert fields["APP_TOKEN"].necessity == "required"
    assert fields["EXACT_KEY"].necessity == "required"
    assert fields["APP_OPTIONAL"].resolution == "default"
    assert fields["FEATURE_ONLY"].necessity == "optional"
    assert "private-default-value" not in schema.model_dump_json()
    assert "private-default-value" in defaults.values()


def test_js_guards_dynamic_names_and_hidden_directories(tmp_path):
    (tmp_path / "config.ts").write_text("""
// process.env.FAKE
const fake = 'process.env.ALSO_FAKE';
const host = process.env.HOST ?? 'localhost';
const token = process.env.TOKEN;
if (!token) throw new Error('missing');
const computed = process.env[prefix + '_TOKEN'];
const mode = import.meta.env.MODE;
""")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "hidden.py").write_text("import os\nx = os.environ['HIDDEN']")
    schema, _ = Analyzer(tmp_path, [unit(javascript=True)], "a" * 64).scan()
    fields = {item.name: item for item in schema.requirements}
    assert fields["TOKEN"].necessity == "required"
    assert fields["HOST"].resolution == "default"
    assert not {"FAKE", "ALSO_FAKE", "HIDDEN", "MODE"} & fields.keys()
    assert schema.ambiguities


def test_local_values_change_invalidates_and_session_loss_recovers_default_redaction(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (candidate / "settings.py").write_text("""
from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env')
    TOKEN: str
    SECRET: str = 'private-default-value'
""")
    (source / ".env").write_text("token=local-secret-one\n")
    execution = SimpleNamespace(
        execution_id="execution",
        identity=SimpleNamespace(source_digest="a" * 64),
        env_schema=EnvFormSchema(source_digest="a" * 64),
        status="awaiting_verification",
        attestation=None,
    )
    run = SimpleNamespace(
        run_id="run",
        plan=SimpleNamespace(units=[unit()]),
        execution=execution,
        reviews={},
        approvals={},
        status="validating",
    )
    manager = EnvironmentManager()
    first = manager.prepare(run, candidate, source)
    fields = {item.name: item for item in first["schema"].requirements}
    assert fields["TOKEN"].resolution == "existing_env"
    assert "local-secret-one" not in first["schema"].model_dump_json()
    generation = manager.session(run).generation
    (source / ".env").write_text("TOKEN=local-secret-two\n")
    again = manager.form(run, source, candidate)
    assert again["schema"].scan_id == first["schema"].scan_id
    assert manager.session(run).generation != generation
    assert "local-secret-one" not in manager.session(run).redact("local-secret-one")
    manager.clear(run.run_id)
    restored = manager.form(run, source, candidate)
    assert fields["TOKEN"].resolution == "missing"
    assert restored["schema_digest"] == first["schema_digest"]
    assert "private-default-value" not in manager.session(run).redact("private-default-value")


def test_dynamic_prefix_and_conflicting_defaults_require_review(tmp_path):
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings, SettingsConfigDict
class Dynamic(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=runtime_prefix())
    UNKNOWN_KEY: str
class One(BaseSettings):
    WORKERS: int = 2
    ENABLED: bool
class Two(BaseSettings):
    WORKERS: int = 5
""")
    schema, _ = Analyzer(tmp_path, [unit()], "a" * 64).scan()
    fields = {item.name: item for item in schema.requirements}
    assert "UNKNOWN_KEY" not in fields
    assert fields["WORKERS"].value_type == "integer"
    assert fields["ENABLED"].value_type == "boolean"
    assert {item.detail for item in schema.ambiguities} == {
        "Settings prefix is dynamic",
        "Environment defaults conflict",
    }


def test_nested_units_do_not_inherit_each_others_sources(tmp_path):
    (tmp_path / "main.py").write_text("import os\nA=os.environ['ROOT_SECRET']\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "config.js").write_text("const u = import.meta.env.VITE_API_URL;\n")
    schema, _ = Analyzer(tmp_path, [unit(), unit("web", javascript=True)], "a" * 64).scan()
    fields = {item.name: item for item in schema.requirements}
    assert [item.unit_id for item in fields["ROOT_SECRET"].consumers] == ["."]
    assert [(item.unit_id, item.phase) for item in fields["VITE_API_URL"].consumers] == [
        ("web", "build")
    ]


@pytest.mark.parametrize("metadata", ["stage1", "example", "both"])
def test_metadata_for_settings_key_produces_one_prompt_and_runtime_value(tmp_path, metadata):
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    TOKEN: str
""")
    app = unit()
    if metadata in {"stage1", "both"}:
        app.config_requirements = [
            ConfigRequirement(name="TOKEN", stage="runtime", presence="referenced")
        ]
    if metadata in {"example", "both"}:
        (tmp_path / ".env.example").write_text("TOKEN=example-secret\n")

    schema, defaults = Analyzer(tmp_path, [app], "a" * 64).scan()

    assert len(schema.requirements) == 1
    field = schema.requirements[0]
    assert (field.name, field.case_sensitive, field.necessity) == ("TOKEN", False, "required")
    expected_kinds = {"python"} | ({"stage1", "example"} if metadata == "both" else {metadata})
    assert {item.kind for item in field.evidence} == expected_kinds
    assert not schema.ambiguities
    assert not defaults
    session = SecretSession(schema)
    session.submit({field.field_id: "submitted-secret"})
    with session.runtime_file(app.id) as path:
        assert path.read_text() == "TOKEN=submitted-secret\n"


def test_metadata_retains_static_name_type_and_private_default(tmp_path):
    (tmp_path / ".env.example").write_text("workers=99\nsecret=example-secret\n")
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    WORKERS: int = 2
    SECRET: str = 'private-static-default'
""")
    app = unit()
    app.config_requirements = [ConfigRequirement(name="workers", stage="runtime")]

    schema, defaults = Analyzer(tmp_path, [app], "a" * 64).scan()

    assert len(schema.requirements) == 2
    fields = {field.name: field for field in schema.requirements}
    workers = fields["WORKERS"]
    assert (workers.case_sensitive, workers.necessity, workers.value_type) == (
        False,
        "optional",
        "integer",
    )
    assert (workers.default_kind, workers.resolution) == ("literal", "default")
    assert defaults[workers.field_id] == "2"
    assert defaults[fields["SECRET"].field_id] == "private-static-default"
    assert "private-static-default" not in schema.model_dump_json()
    assert not schema.ambiguities


def test_metadata_keeps_case_sensitive_keys_and_phases_distinct(tmp_path):
    (tmp_path / ".env.example").write_text("TOKEN=example\ntoken=example\n")
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True)
    TOKEN: str
    token: str
""")
    app = unit()
    app.config_requirements = [
        ConfigRequirement(name="TOKEN", stage="build_time"),
        ConfigRequirement(name="TOKEN", stage="runtime"),
    ]

    schema, _ = Analyzer(tmp_path, [app], "a" * 64).scan()

    assert {(field.name, field.consumers[0].phase) for field in schema.requirements} == {
        ("TOKEN", "runtime"),
        ("token", "runtime"),
        ("TOKEN", "build"),
    }
    assert len(schema.requirements) == 3
    assert all(field.case_sensitive for field in schema.requirements)
    assert not schema.ambiguities


def test_metadata_does_not_mask_conflicting_static_defaults(tmp_path):
    (tmp_path / ".env.example").write_text("workers=99\n")
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings
class One(BaseSettings):
    WORKERS: int = 2
class Two(BaseSettings):
    WORKERS: int = 5
""")
    app = unit()
    app.config_requirements = [ConfigRequirement(name="WORKERS", stage="runtime")]

    schema, _ = Analyzer(tmp_path, [app], "a" * 64).scan()

    assert len(schema.requirements) == 1
    assert {item.detail for item in schema.ambiguities} == {"Environment defaults conflict"}
    assert {item.kind for item in schema.requirements[0].evidence} == {
        "python",
        "stage1",
        "example",
    }


def test_metadata_keeps_scopes_and_consumers_distinct(tmp_path):
    (tmp_path / "settings.py").write_text("""
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    TOKEN: str
""")
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / ".env.example").write_text("TOKEN=example\n")
    worker = unit("worker")
    worker.config_requirements = [ConfigRequirement(name="TOKEN", stage="runtime")]

    schema, _ = Analyzer(tmp_path, [unit(), worker], "a" * 64).scan()

    assert len(schema.requirements) == 2
    assert {
        (field.scope, field.consumers[0].unit_id, field.case_sensitive, field.necessity)
        for field in schema.requirements
    } == {(".", ".", False, "required"), ("worker", "worker", True, "optional")}
    assert not schema.ambiguities
