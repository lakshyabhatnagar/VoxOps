"""Volatile environment values and their private injection boundary."""

import contextlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from io import StringIO
from pathlib import Path
from uuid import uuid4

from dotenv.parser import parse_stream

from voxops.core.errors import AppError
from voxops.domain.execution_models import EnvEvidence, EnvFormSchema, EnvRequirement
from voxops.storage.artifact_store import digest


def read_env_file(
    root: Path, relative: str, *, max_bytes: int = 65536
) -> tuple[dict[str, str], tuple[int, int, int, int] | None]:
    """Read a recognized env file beneath a trusted root without following links."""
    parts = Path(relative).parts
    recognized = {".env", ".env.local", ".env.production", ".env.production.local"}
    if (
        not parts
        or parts[-1] not in recognized
        or any(part in {".", ".."} for part in parts)
        or Path(relative).is_absolute()
    ):
        raise AppError("unsafe_env_file", "Environment file path is not recognized.", 409)
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            descriptors.append(directory)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {}, None
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError
        data = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        version = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if len(data) > max_bytes or version != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        values = {}
        for binding in parse_stream(StringIO(data.decode("utf-8"))):
            if binding.error:
                raise ValueError
            if binding.key is None:
                continue
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding.key)
                or binding.value is None
                or "\x00" in binding.value
            ):
                raise ValueError
            if "${" in binding.value:
                raise AppError(
                    "env_interpolation_unsupported",
                    "Environment interpolation cannot be safely read; use literal values.",
                    409,
                )
            values[binding.key] = binding.value
        return values, version
    except (OSError, ValueError, UnicodeError):
        raise AppError(
            "unsafe_env_file", "Environment file could not be safely read.", 409
        ) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _valid_value(item: EnvRequirement, value: str) -> bool:
    if not value.strip() or len(value) > item.max_length or any(c in value for c in "\x00\r\n"):
        return False
    if re.fullmatch(
        r"(?i)(?:change[_ -]?me|replace[_ -]?me|placeholder|your[_ -].*|<[^>]+>)", value.strip()
    ):
        return False
    if (item.necessity == "required" or item.visibility == "secret") and value.strip().lower() in {
        "example",
        "xxx",
    }:
        return False
    if item.value_type in {"integer", "port"}:
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            return False
        if item.value_type == "port" and not 1 <= int(value) <= 65535:
            return False
    if item.value_type == "boolean" and value.lower() not in {"true", "false", "1", "0"}:
        return False
    if item.value_type == "url":
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(value)
            if not parsed.scheme or not parsed.hostname or not parsed.netloc:
                return False
            _ = parsed.port
        except ValueError:
            return False
    return True


def _private_values(schema, values, default_ids=()):
    return {
        values[item.field_id]
        for item in schema.requirements
        if item.field_id in values
        and values[item.field_id]
        and item.visibility != "public"
        and not (
            item.visibility == "unknown"
            and item.field_id in default_ids
            and item.value_type in {"integer", "port", "boolean"}
        )
    }


class SecretSession:
    """Owns values only in process memory; never serialize this object."""

    def __init__(
        self,
        schema: EnvFormSchema,
        values: Mapping[str, str] | None = None,
        defaults: Mapping[str, str] | None = None,
    ):
        self.schema = schema
        self._defaults = dict(defaults or {})
        self._values = dict(values or {})
        self._supplied = set(values or {})
        self._redactions = _private_values(
            schema, self._defaults | self._values, self._defaults.keys() - self._supplied
        )
        self.generation = uuid4().hex

    def __repr__(self) -> str:
        return "<SecretSession redacted>"

    def __reduce__(self):
        raise TypeError("Secret sessions cannot be serialized")

    def submit(self, values: Mapping[str, str]) -> None:
        fields = {item.field_id: item for item in self.schema.requirements}
        if set(values) - fields.keys() or any(not isinstance(v, str) for v in values.values()):
            raise AppError("invalid_env_values", "Environment submission is invalid.", 422)
        candidate = self._values | dict(values)
        if any(not _valid_value(fields[key], value) for key, value in values.items()):
            raise AppError("invalid_env_values", "Environment submission is invalid.", 422)
        if any(
            item.necessity == "required"
            and item.resolution != "default"
            and not candidate.get(item.field_id)
            for item in fields.values()
        ):
            raise AppError("invalid_env_values", "Required environment values are missing.", 422)
        private = _private_values(
            self.schema,
            self._defaults | candidate,
            self._defaults.keys() - (self._supplied | values.keys()),
        )
        if any(
            secret in candidate[item.field_id]
            for item in fields.values()
            if item.visibility == "public" and item.field_id in candidate
            for secret in private
        ):
            raise AppError(
                "invalid_env_values", "Public values cannot contain private configuration.", 422
            )
        if candidate != self._values or set(values) - self._supplied:
            self._redactions.update(private)
            self._supplied.update(values)
            self._values = candidate
            self.generation = uuid4().hex

    def redact(self, text: str) -> str:
        values = set(self._redactions)
        for value in self._redactions:
            # Docker drains complete lines, while structured diagnostics JSON-escape values.
            values.update(value.splitlines())
            values.update(
                json.dumps(part, ensure_ascii=ascii_only)[1:-1]
                for part in (value, *value.splitlines())
                for ascii_only in (True, False)
            )
        for value in sorted(values, key=len, reverse=True):
            if value:
                text = text.replace(value, "[REDACTED]")
        text = re.sub(
            r"\b(?:https?|postgres(?:ql)?|redis|mysql)://[^\s]+", "[REDACTED URL]", text, flags=re.I
        )
        return re.sub(r"(?i)\b(?:bearer\s+|token[=:]\s*)[^\s,;]+", "[REDACTED TOKEN]", text)

    def values_for(self, unit_id: str, phase: str, visibility: str) -> dict[str, str]:
        return {
            item.name: self._values[item.field_id]
            for item in self.schema.requirements
            if item.visibility == visibility
            and item.field_id in self._values
            and any(c.unit_id == unit_id and c.phase == phase for c in item.consumers)
        }

    def contains(self, field_id: str) -> bool:
        return field_id in self._values

    @contextlib.contextmanager
    def runtime_file(self, unit_id: str) -> Iterator[Path]:
        values = {}
        for visibility in ("public", "secret", "unknown"):
            values.update(self.values_for(unit_id, "runtime", visibility))
        with tempfile.TemporaryDirectory(prefix="voxops-env-") as directory:
            path = Path(directory) / "runtime.env"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for name, value in sorted(values.items()):
                    stream.write(f"{name}={value}\n")
            yield path


def schema_digest(schema: EnvFormSchema) -> str:
    return digest(
        schema.model_dump(mode="json", exclude={"requirements": {"__all__": {"resolution"}}})
    )


class EnvironmentManager:
    """Owns session authority for linked execution records in this process."""

    def __init__(self):
        self._sessions = {}

    def _scan(self, run, candidate_root: Path):
        from voxops.execution.environment_analysis import Analyzer

        analyzer = Analyzer(candidate_root, run.plan.units, run.execution.identity.source_digest)
        schema, defaults = analyzer.scan()
        known_defaults = dict(defaults)
        for item in schema.requirements:
            if item.field_id in defaults and not _valid_value(item, defaults[item.field_id]):
                defaults.pop(item.field_id)
                item.resolution = "ambiguous"
                schema.ambiguities.append(
                    EnvEvidence(
                        path=item.evidence[0].path,
                        line=item.evidence[0].line,
                        kind=item.evidence[0].kind,
                        detail="Static default requires review",
                    )
                )
        return schema, defaults, known_defaults, analyzer.extra_defaults

    def prepare(self, run, candidate_root: Path, source_root: Path):
        execution = run.execution
        if execution is None:
            raise AppError(
                "verification_not_prepared", "Execution verification is not prepared.", 409
            )
        if execution.env_schema.scan_id:
            return self.form(run, source_root, candidate_root)
        schema, defaults, known_defaults, extra_defaults = self._scan(run, candidate_root)
        values, versions = self._local_values(schema, source_root)
        self._reject_public_leaks(schema, defaults | values, defaults.keys() - values.keys())
        session = SecretSession(schema, values, defaults)
        session._redactions.update(_private_values(schema, known_defaults, known_defaults.keys()))
        session._redactions.update(extra_defaults)
        for item in schema.requirements:
            if item.field_id in values:
                item.resolution = "existing_env"
        execution.env_schema = schema
        self._sessions[run.run_id] = (
            execution.execution_id,
            schema_digest(schema),
            session,
            versions,
        )
        return {"schema": schema, "schema_digest": schema_digest(schema)}

    def _local_values(self, schema, source_root):
        values, versions = {}, {}
        for scope in sorted({item.scope for item in schema.requirements}):
            merged = {}
            for filename in schema.env_files.get(scope, []):
                relative = str(Path(scope) / filename) if scope != "." else filename
                found, version = read_env_file(source_root, relative)
                versions[relative] = version
                merged.update(found)
            for item in schema.requirements:
                if item.scope != scope:
                    continue
                lookup = (
                    merged
                    if item.case_sensitive
                    else {key.lower(): value for key, value in merged.items()}
                )
                name = item.name if item.case_sensitive else item.name.lower()
                if name in lookup and _valid_value(item, lookup[name]):
                    values[item.field_id] = lookup[name]
        return values, versions

    @staticmethod
    def _reject_public_leaks(schema, values, default_ids=()):
        private = _private_values(schema, values, default_ids)
        if any(
            secret in values[item.field_id]
            for item in schema.requirements
            if item.visibility == "public" and item.field_id in values
            for secret in private
        ):
            raise AppError(
                "invalid_env_values", "Public configuration contains private values.", 409
            )

    def form(self, run, source_root: Path, candidate_root: Path):
        execution = run.execution
        if execution is None or not execution.env_schema.scan_id:
            raise AppError(
                "verification_not_prepared", "Environment verification is not prepared.", 409
            )
        schema = execution.env_schema
        record = self._sessions.get(run.run_id)
        if record and (record[0] != execution.execution_id or record[1] != schema_digest(schema)):
            record = None
            self._sessions.pop(run.run_id, None)
        if record:
            try:
                values, versions = self._local_values(schema, source_root)
            except AppError:
                self._sessions.pop(run.run_id, None)
                self._invalidate(run)
                raise
            if versions != record[3]:
                try:
                    self._reject_public_leaks(
                        schema,
                        record[2]._defaults | values,
                        record[2]._defaults.keys() - values.keys(),
                    )
                except AppError:
                    self._sessions.pop(run.run_id, None)
                    self._invalidate(run)
                    raise
                session = SecretSession(schema, values, record[2]._defaults)
                session._redactions.update(record[2]._redactions)
                self._sessions[run.run_id] = (
                    execution.execution_id,
                    schema_digest(schema),
                    session,
                    versions,
                )
                for item in schema.requirements:
                    item.resolution = (
                        "existing_env"
                        if item.field_id in values
                        else "default"
                        if item.field_id in session._defaults or item.default_kind == "none"
                        else "missing"
                    )
                self._invalidate(run)
        else:
            self._invalidate(run)
            for item in schema.requirements:
                if item.resolution in {"existing_env", "supplied", "default"}:
                    item.resolution = "missing"
            fresh, defaults, known_defaults, extra_defaults = self._scan(run, candidate_root)
            fresh.scan_id = schema.scan_id
            fresh.revision = schema.revision
            if schema_digest(fresh) != schema_digest(schema):
                raise AppError("env_form_stale", "Environment schema changed; prepare again.", 409)
            local, versions = self._local_values(schema, source_root)
            session = SecretSession(schema, defaults=defaults)
            session._redactions.update(
                _private_values(schema, known_defaults, known_defaults.keys())
            )
            session._redactions.update(extra_defaults)
            session._redactions.update(_private_values(schema, local))
            for item in schema.requirements:
                if item.field_id in defaults or item.default_kind == "none":
                    item.resolution = "default"
            self._sessions[run.run_id] = (
                execution.execution_id,
                schema_digest(schema),
                session,
                versions,
            )
        return {"schema": schema, "schema_digest": schema_digest(schema)}

    def submit(
        self,
        run,
        source_root: Path,
        candidate_root: Path,
        expected_digest: str,
        values: Mapping[str, str],
    ):
        form = self.form(run, source_root, candidate_root)
        if form["schema_digest"] != expected_digest:
            raise AppError("env_form_stale", "Environment form changed; reload it.", 409)
        record = self._sessions.get(run.run_id)
        local = {}
        if record is None:
            local, versions = self._local_values(run.execution.env_schema, source_root)
            self._reject_public_leaks(run.execution.env_schema, local)
            session = SecretSession(run.execution.env_schema, local)
            record = (run.execution.execution_id, expected_digest, session, versions)
        session = record[2]
        generation = session.generation
        session.submit(values)
        if local or session.generation != generation:
            for item in run.execution.env_schema.requirements:
                if item.field_id in values:
                    item.resolution = "supplied"
                elif item.field_id in local:
                    item.resolution = "existing_env"
            self._invalidate(run)
        self._sessions[run.run_id] = record
        return self.form(run, source_root, candidate_root)

    def clear(self, run_id: str) -> None:
        self._sessions.pop(run_id, None)

    def authority(self, run, source_root: Path, candidate_root: Path, expected_generation=None):
        """Caller owns the project lock and persists any invalidation on exit."""
        self.form(run, source_root, candidate_root)
        session = self.session(run)
        if run.execution.env_schema.ambiguities or any(
            item.necessity == "unknown"
            or item.resolution == "ambiguous"
            or (item.necessity == "required" and item.resolution == "missing")
            for item in run.execution.env_schema.requirements
        ):
            raise AppError("env_unresolved", "Environment configuration is incomplete.", 409)
        if expected_generation is not None and expected_generation != session.generation:
            raise AppError("env_session_changed", "Environment configuration changed.", 409)
        return session

    def session(self, run):
        record = self._sessions.get(run.run_id)
        if (
            record is None
            or run.execution is None
            or record[:2] != (run.execution.execution_id, schema_digest(run.execution.env_schema))
        ):
            raise AppError("env_session_lost", "Environment values must be supplied again.", 409)
        return record[2]

    @staticmethod
    def _invalidate(run):
        run.execution.attestation = None
        run.execution.status = "waiting_for_env"
        ids = {
            key for key, review in run.reviews.items() if review.purpose == "artifact_publication"
        }
        for key in ids:
            run.reviews.pop(key, None)
            run.approvals.pop(key, None)
        if run.status in {"awaiting_review", "approved"}:
            run.status = "validating"
