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


# Read a supported local .env file and turn its entries into name/value pairs.
# Check its size and file type, and refuse links or values that depend on substitution.
# Also return file details so later calls can notice that someone changed it.
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


# Check whether one configuration value is usable for this field.
# Reject empty values, obvious placeholders, and values with unsafe line breaks.
# For ports, numbers, switches, and URLs, check the expected format too.
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


# Pick out the values that must be hidden from saved output.
# Include secret and unknown fields, rather than assuming an unknown value is public.
# Do not hide ordinary numeric or boolean defaults merely because their visibility is unknown.
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


# Hold the actual configuration values in this Python process's memory.
# For example, keep a submitted database password here while the saved form contains only its
# name.
# Provide the values to the right app and hide them when preparing output.
class SecretSession:
    """Owns values only in process memory; never serialize this object."""

    # Remember existing values and code defaults separately.
    # Build the first set of strings to hide from output.
    # Create random change markers so checks can detect replacement values without saving their
    # contents.
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
        self.unit_generations = {
            consumer.unit_id: uuid4().hex
            for item in schema.requirements
            if item.field_id in self._values
            for consumer in item.consumers
        }

    # Return a harmless description when this object is printed.
    # Do not include its stored passwords or other values.
    # This avoids exposing configuration during ordinary Python debugging.
    def __repr__(self) -> str:
        return "<SecretSession redacted>"

    # Refuse Python's attempt to serialize this session.
    # The object holds real configuration values that belong only in memory.
    # Raising an error prevents pickle from writing those values into a file.
    def __reduce__(self):
        raise TypeError("Secret sessions cannot be serialized")

    # Check a complete candidate set before accepting any submitted changes.
    # Make sure required values exist and that public fields do not contain private values.
    # Only then replace the values and update change markers for the affected apps.
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
            changed = {key for key in candidate if candidate.get(key) != self._values.get(key)}
            for item in self.schema.requirements:
                if item.field_id in changed:
                    for consumer in item.consumers:
                        self.unit_generations[consumer.unit_id] = uuid4().hex
            self._redactions.update(private)
            self._supplied.update(values)
            self._values = candidate
            self.generation = uuid4().hex

    # Replace known private values with a redaction marker in output text.
    # Also cover escaped and multiline forms that can appear in diagnostic messages.
    # Hide URL and token-shaped text as an additional check before output is retained.
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

    # Return configuration for just one app, phase, and visibility group.
    # A frontend build and an API process may need different values.
    # This prevents each Docker operation from receiving every value in the session.
    def values_for(self, unit_id: str, phase: str, visibility: str) -> dict[str, str]:
        return {
            item.name: self._values[item.field_id]
            for item in self.schema.requirements
            if item.visibility == visibility
            and item.field_id in self._values
            and any(c.unit_id == unit_id and c.phase == phase for c in item.consumers)
        }

    # Check whether this session currently holds a value for one form field.
    # Return only yes or no, without returning the value itself.
    # The runner uses this when deciding whether required configuration is present.
    def contains(self, field_id: str) -> bool:
        return field_id in self._values

    # Create a temporary file containing one app's runtime environment values.
    # Restrict access to its directory and file while Docker reads it.
    # Remove it when the surrounding operation finishes, including when that operation raises an
    # error.
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


# Calculate a comparison code for the form's structure.
# Leave out whether a field is currently supplied, missing, or defaulted.
# Submitting values therefore does not make an otherwise unchanged form look like a different
# form.
def schema_digest(schema: EnvFormSchema) -> str:
    return digest(
        schema.model_dump(mode="json", exclude={"requirements": {"__all__": {"resolution"}}})
    )


# Connect each saved execution run to its in-memory configuration session.
# Read existing .env files, build forms, and accept missing values from the user.
# When memory is lost, recover local values and defaults but not temporary user submissions.
class EnvironmentManager:
    """Owns session authority for linked execution records in this process."""

    # Start with no in-memory sessions.
    # Sessions are indexed by run ID so different runs keep their values separate.
    # A fresh manager must reconstruct local configuration before execution can continue.
    def __init__(self, max_file_bytes: int = 1048576):
        self._sessions = {}
        self.max_file_bytes = max_file_bytes

    # Calculate a comparison code for the app sources used to discover configuration.
    # Leave out generated deployment files unless the task edits application source.
    # Changing app code can change its configuration needs, so old forms must be checked again.
    def source_digest(self, run, candidate_root: Path):
        from voxops.repository.snapshot import capture_source

        _, inventory = capture_source(candidate_root, self.max_file_bytes)
        generated = {
            task.target_path
            for task in run.plan.tasks.values()
            if task.artifact_type != "source_patch"
        }
        return digest({key: value for key, value in inventory.items() if key not in generated})

    # Ask the static scanner which environment names and defaults the app uses.
    # Reject unusable defaults instead of silently treating them as working configuration.
    # Keep private defaults available for redaction without putting their contents into the saved
    # form.
    def _scan(self, run, candidate_root: Path):
        from voxops.execution.environment_analysis import Analyzer

        analyzer = Analyzer(
            candidate_root, run.plan.units, self.source_digest(run, candidate_root),
            max_file_bytes=self.max_file_bytes,
        )
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

    # Build the first environment form and in-memory session for a run.
    # Combine discovered defaults with valid values from the original repository's local files.
    # If a form already exists, use the refresh path instead of starting another unrelated
    # session.
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

    # Read each scope's selected .env files in their configured order.
    # Match names using the case rules discovered for each field, and keep only valid values.
    # Return file versions too so edits to local configuration can invalidate old checks.
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

    # Check that a public value does not contain a known private value.
    # For example, a frontend URL must not include a submitted secret.
    # Reject the configuration before those public values can be passed into a build.
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

    # Refresh a saved form against the current files and in-memory session.
    # If local files changed, replace the session and invalidate verification that used the old
    # values.
    # If the session was lost, reread local values and defaults while leaving user-only secrets
    # missing.
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
                changed = {
                    key
                    for key in set(values) | set(record[2]._values)
                    if values.get(key) != record[2]._values.get(key)
                }
                affected = {
                    c.unit_id
                    for item in schema.requirements
                    if item.field_id in changed
                    for c in item.consumers
                }
                for unit, generation in record[2].unit_generations.items():
                    if unit not in affected:
                        session.unit_generations[unit] = generation
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
            self._reject_public_leaks(schema, defaults | local, defaults.keys() - local.keys())
            session = SecretSession(schema, local, defaults)
            session._redactions.update(
                _private_values(schema, known_defaults, known_defaults.keys())
            )
            session._redactions.update(extra_defaults)
            for item in schema.requirements:
                if item.field_id in local:
                    item.resolution = "existing_env"
                elif item.field_id in defaults or item.default_kind == "none":
                    item.resolution = "default"
            self._sessions[run.run_id] = (
                execution.execution_id,
                schema_digest(schema),
                session,
                versions,
            )
        return {"schema": schema, "schema_digest": schema_digest(schema)}

    # Refresh the form before accepting the user's values.
    # Reject an outdated form or invalid submission before altering the session.
    # Mark accepted fields as supplied and invalidate results that used different configuration.
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

    # Forget the in-memory session for one run.
    # This discards temporary user-entered values without writing them anywhere.
    # A later refresh can recover local files and code defaults, but must request lost user values
    # again.
    def clear(self, run_id: str) -> None:
        self._sessions.pop(run_id, None)

    # Confirm that this run has a current session and no unresolved required configuration.
    # Optionally compare the expected change markers with the current ones.
    # The execution and publication paths use this to reject results tied to lost or changed
    # values.
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
        if expected_generation is not None and expected_generation != (
            session.unit_generations
            if isinstance(expected_generation, dict)
            else session.generation
        ):
            raise AppError("env_session_changed", "Environment configuration changed.", 409)
        return session

    # Retrieve a session only if its run, execution ID, and form still match.
    # Attach the run's current form object so field states are up to date.
    # If memory was lost or belongs to an older execution, require configuration recovery instead.
    def session(self, run):
        record = self._sessions.get(run.run_id)
        if (
            record is None
            or run.execution is None
            or record[:2] != (run.execution.execution_id, schema_digest(run.execution.env_schema))
        ):
            raise AppError("env_session_lost", "Environment values must be supplied again.", 409)
        record[2].schema = run.execution.env_schema
        return record[2]

    # Clear the saved verification result and any final publication review and approval.
    # Move the run back to configuration checking when old values are no longer trustworthy.
    # This prevents a branch from being published using checks performed with different
    # configuration.
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
