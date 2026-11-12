"""Derive executable verification contracts from the actual Stage 2 candidate."""

import json
import re
import shlex
from pathlib import Path, PurePosixPath
from uuid import uuid4

from voxops.artifacts.security import safe_read
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactPlan
from voxops.domain.execution_models import (
    ExecutionPlan,
    ExecutionTask,
    ProbeSpec,
    UnitExecutionSpec,
)
from voxops.domain.models import Framework, Role
from voxops.execution.candidate import execution_policy_fingerprint
from voxops.execution.contexts import FrozenContext, freeze_context, remove_context
from voxops.repository.snapshot import capture_source
from voxops.storage.artifact_store import digest

_LOCKS = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}


# Check that a planned path stays inside the repository.
# Reject absolute paths, parent-directory escapes, and unsupported characters.
# Return the checked path for later file selection and Docker commands.
def _path(value: str, *, allow_root: bool = True) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or str(path) != value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or (value == "." and not allow_root)
    ):
        raise AppError("INVALID_ARTIFACT", "Execution input path is unsafe or unknown.", 409)
    return value


# Read a package manifest as a JSON object from the candidate files.
# Use the existing safe file reader and reject missing or invalid JSON.
# The planner needs actual package scripts and settings before choosing execution commands.
def _manifest(source: Path, path: str) -> dict:
    try:
        data = safe_read(source, path)
        result = json.loads(data) if data is not None else None
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise AppError(
            "INVALID_ARTIFACT", "A required package manifest is unavailable.", 409
        ) from None


# Turn a supported project script into the command arguments that Docker will execute.
# Use the package-manager command when it matches the named script.
# Explicit custom commands are kept for execution inside the container, with basic input limits.
def _script(command: str | None, manager: str, scripts: dict, name: str) -> list[str]:
    if not command or not isinstance(scripts.get(name), str) or not scripts[name].strip():
        raise AppError("INVALID_ARTIFACT", f"A known {name} script is required.", 409)
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    if argv not in ([manager, "run", name], [manager, name]):
        # Explicit commands run only inside a bounded container. Their syntax is not a host shell.
        if any(c in command for c in "\x00\r\n") or len(command) > 2000:
            raise AppError("INVALID_ARTIFACT", "The explicit project command is invalid.", 409)
        return ["sh", "-c", command]
    return [manager, "run", name]


# Work out how a Vite or Next.js app installs, builds, and starts.
# Read its real package manager, lockfile, scripts, workspace, and output settings.
# Return the commands and required paths used to verify that app's chosen strategy.
def _node_spec(source: Path, unit, strategy: str, settings: Settings, inventory) -> dict:
    root = _path(unit.root_path)
    workspace = _path(unit.runtime.workspace_root or root)
    if not PurePosixPath(root).is_relative_to(workspace):
        raise AppError("INVALID_ARTIFACT", "Unit is outside its declared workspace.", 409)
    manifest_path = str(PurePosixPath(root) / "package.json")
    manifest = _manifest(source, manifest_path)
    manager = unit.runtime.package_manager or "npm"
    if manager not in _LOCKS:
        raise AppError(
            "INVALID_ARTIFACT", "Package manager is unsupported for local verification.", 409
        )
    lock = unit.runtime.lockfile_path
    if lock:
        _path(lock, allow_root=False)
        if PurePosixPath(lock).name != _LOCKS[manager] or lock not in inventory:
            raise AppError(
                "INVALID_ARTIFACT", "Declared package lock is unavailable or inconsistent.", 409
            )
        workspace = str(PurePosixPath(lock).parent)
    else:
        for parent in [PurePosixPath(root), *PurePosixPath(root).parents]:
            candidate = str(parent / _LOCKS[manager])
            if candidate in inventory:
                lock, workspace = candidate, str(parent)
                break
    if not PurePosixPath(root).is_relative_to(workspace):
        raise AppError("INVALID_ARTIFACT", "Package lock lies outside unit ancestry.", 409)
    workspace_manifest = _manifest(source, str(PurePosixPath(workspace) / "package.json"))
    declared_manager = workspace_manifest.get("packageManager", manifest.get("packageManager", ""))
    prefix = []
    if manager != "npm":
        if not isinstance(declared_manager, str) or not re.fullmatch(
            rf"{manager}@[0-9]+\.[0-9]+\.[0-9]+(?:\+[A-Za-z0-9.]+)?", declared_manager
        ):
            raise AppError(
                "INVALID_ARTIFACT", "pnpm/yarn requires an exact packageManager version.", 409
            )
        prefix = ["corepack", manager]
    if manager == "npm":
        install = ["npm", "ci" if lock else "install", "--no-audit", "--no-fund"]
    elif manager == "pnpm":
        install = [*prefix, "install", *(["--frozen-lockfile"] if lock else [])]
    else:
        major = int(declared_manager.split("@", 1)[1].split(".", 1)[0])
        install = [
            *prefix,
            "install",
            *(["--frozen-lockfile" if major == 1 else "--immutable"] if lock else []),
        ]
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict):
        raise AppError("INVALID_ARTIFACT", "Package scripts must be an object.", 409)
    build = _script(unit.runtime.build_command, manager, scripts, "build")
    if prefix and build[0] == manager:
        build = ["corepack", *build]
    selected = {root, *unit.shared_packages, *unit.runtime.dependency_files}
    ancestors = [PurePosixPath(root), *PurePosixPath(root).parents]
    for parent in ancestors:
        if not parent.is_relative_to(workspace):
            continue
        for filename in (
            "package.json",
            "pnpm-workspace.yaml",
            ".npmrc",
            ".yarnrc.yml",
            *_LOCKS.values(),
        ):
            candidate = str(parent / filename)
            if candidate in inventory:
                selected.add(candidate)
    # Workspace installers resolve manifests for every declared member, even independent units.
    manifests = {}
    for candidate in inventory:
        path = PurePosixPath(candidate)
        if path.name == "package.json" and path.is_relative_to(workspace):
            data = _manifest(source, candidate)
            manifests[str(path.parent)] = data
            selected.add(candidate)
    pending = [root, *unit.shared_packages]
    visited = set()
    while pending:
        base = _path(pending.pop())
        if base in visited:
            continue
        visited.add(base)
        selected.add(base)
        data = manifests.get(base, {})
        deps = data.get("dependencies", {}) | data.get("devDependencies", {})
        for name, version in deps.items():
            if isinstance(version, str) and version.startswith(("workspace:", "file:", "link:")):
                if version.startswith("workspace:"):
                    matches = [p for p, item in manifests.items() if item.get("name") == name]
                else:
                    target = PurePosixPath(base) / version.split(":", 1)[1]
                    # Resolve lexical parent segments without following repository symlinks.
                    parts = []
                    for part in target.parts:
                        if part == "..":
                            if not parts:
                                raise AppError(
                                    "INVALID_ARTIFACT", "Local dependency escapes workspace.", 409
                                )
                            parts.pop()
                        elif part != ".":
                            parts.append(part)
                    matches = ["/".join(parts) or "."]
                if len(matches) != 1 or matches[0] not in manifests:
                    raise AppError(
                        "INVALID_ARTIFACT", "Local package dependency cannot be resolved.", 409
                    )
                pending.extend(matches)
    for item in selected:
        _path(item)
        if not PurePosixPath(item).is_relative_to(workspace):
            raise AppError("INVALID_ARTIFACT", "Shared inputs lie outside the workspace.", 409)
    output = unit.runtime.output_directory
    if strategy == "static":
        if unit.framework == Framework.NEXT and unit.role != Role.STATIC:
            raise AppError(
                "INVALID_ARTIFACT", "Next.js static export lacks static-only evidence.", 409
            )
        if not output:
            raise AppError("INVALID_ARTIFACT", "Static output directory is unknown.", 409)
        _path(output, allow_root=False)
    start = []
    if strategy == "native":
        if unit.framework != Framework.NEXT or unit.role == Role.STATIC:
            raise AppError(
                "INVALID_ARTIFACT", "Native verification requires Next.js server evidence.", 409
            )
        command = unit.runtime.start_command
        script = scripts.get("start", "")
        if not isinstance(script, str):
            script = ""
        try:
            words = shlex.split(script)
        except ValueError:
            words = []
        if words[:2] == ["next", "start"] and not any(
            token in script for token in ("&&", "||", ";", "|", "$", "`", "\n", ">", "<")
        ):
            if any(word in words for word in ("-p", "--port", "-H", "--hostname")) or any(
                word.startswith(("--port=", "--hostname=")) for word in words
            ):
                raise AppError(
                    "INVALID_ARTIFACT", "Next start script has conflicting bind flags.", 409
                )
            start = _script(command, manager, scripts, "start")
            if start[:2] == ["sh", "-c"]:
                raise AppError("INVALID_ARTIFACT", "Native start command is ambiguous.", 409)
            if prefix:
                start = ["corepack", *start]
            start += [
                *(["--"] if manager == "npm" else []),
                "--hostname",
                "0.0.0.0",
                "--port",
                "3000",
            ]
        elif words == ["node", ".next/standalone/server.js"] and command in (
            f"{manager} run start",
            f"{manager} start",
        ):
            start = words
        else:
            raise AppError(
                "INVALID_ARTIFACT", "Native Next.js start mode is unsupported or ambiguous.", 409
            )
    return dict(
        context_path=workspace,
        selected_paths=sorted(selected),
        required_paths=sorted({manifest_path, *([lock] if lock else [])}),
        install_workdir="/workspace",
        command_workdir=str(
            PurePosixPath("/workspace") / PurePosixPath(root).relative_to(workspace)
        ),
        builder_image=settings.execution_node_image,
        install_argv=install,
        build_argv=build,
        start_argv=start,
        output_directory=output,
    )


# Read the planned Dockerfile and determine how its server can be checked.
# Validate the supported startup command, port, and required input paths.
# Reject unclear cases instead of inventing a runtime command that the image may not support.
def _container_spec(
    source: Path, artifact_plan: ArtifactPlan, unit, probe: ProbeSpec | None
) -> dict:
    tasks = [
        task
        for task in artifact_plan.tasks.values()
        if (
            task.unit_id == unit.id
            and task.artifact_type == "dockerfile"
            and task.operation != "skip"
        )
    ]
    if len(tasks) != 1:
        raise AppError(
            "INVALID_ARTIFACT", "Container verification needs one owned Dockerfile task.", 409
        )
    task = tasks[0]
    context = _path(task.build_context)
    target = _path(task.target_path, allow_root=False)
    if not PurePosixPath(target).is_relative_to(context):
        raise AppError(
            "INVALID_ARTIFACT", "Dockerfile lies outside its planned build context.", 409
        )
    try:
        data = safe_read(source, target)
        text = data.decode() if data else ""
    except UnicodeError:
        text = ""
    if not text:
        raise AppError("INVALID_ARTIFACT", "Planned Dockerfile is unavailable.", 409)
    final = re.split(r"(?im)^\s*FROM\s+", text.replace("\\\n", " "))[-1]
    instructions = dict(re.findall(r"(?im)^\s*(CMD|ENTRYPOINT)\s+(.+)$", final))
    commands = " ".join(instructions.values())
    if any(command in commands for command in unit.runtime.prerequisite_commands):
        raise AppError(
            "INVALID_ARTIFACT", "Image command includes a prohibited startup prerequisite.", 409
        )
    if unit.framework == Framework.VITE and probe and probe.health_contract:
        raise AppError(
            "INVALID_ARTIFACT", "Static container verification proves HTTP reachability only.", 409
        )
    if unit.framework == Framework.FASTAPI and not re.search(
        r"(?:--host[\s\",=]+0\.0\.0\.0|(?:--bind|-b)[\s\",=]+0\.0\.0\.0:)",
        commands,
    ):
        raise AppError(
            "INVALID_ARTIFACT", "FastAPI image command must bind 0.0.0.0 inside Docker.", 409
        )
    declared = set(
        int(value) for value in re.findall(r"(?im)^\s*EXPOSE\s+(\d+)(?:/tcp)?\s*$", final)
    )
    flags = re.findall(r"--port[\s\",=]+(\d+)|(?:--bind|-b)[\s\",=]+0\.0\.0\.0:(\d+)", commands)
    configured = {int(a or b) for a, b in flags}
    port_from_env = bool(re.search(r"\$\{?PORT\b", commands))
    env_ports = re.findall(r"(?im)^\s*ENV\s+PORT[=\s]+[\"']?(\d+)", final)
    if port_from_env:
        configured = {int(env_ports[-1])} if env_ports else declared
    if not configured and not port_from_env and re.search(r"\buvicorn\b", commands):
        configured = {8000}
    if not configured and unit.framework == Framework.NEXT:
        configured = {int(env_ports[-1])} if env_ports else {3000}
        port_from_env = True
    ports = configured or declared
    if len(ports) != 1 or any(not 1 <= p <= 65535 for p in ports):
        raise AppError(
            "INVALID_ARTIFACT", "Container runtime port is ambiguous or unsupported.", 409
        )
    port = ports.pop()
    if declared and port not in declared:
        raise AppError("INVALID_ARTIFACT", "Container command and EXPOSE ports disagree.", 409)
    if probe and probe.port != port:
        raise AppError(
            "INVALID_ARTIFACT", "Probe port disagrees with the actual image start contract.", 409
        )
    return dict(
        context_path=context,
        dockerfile_path=str(PurePosixPath(target).relative_to(context)),
        required_paths=sorted({target, *task.required_paths}),
        probe=probe or ProbeSpec(port=port),
        port_from_env=port_from_env,
    )


# Turn the existing Stage 2 file plan into concrete verification jobs.
# Choose the supported container, native-server, or static-build path for each app.
# Record job dependencies and input comparison codes so later results belong to these exact
# inputs.
def build_execution_plan(
    source: Path,
    artifact_plan: ArtifactPlan,
    settings: Settings,
    *,
    probes: dict[str, ProbeSpec] | None = None,
) -> ExecutionPlan:
    probes = probes or {}
    _, inventory = capture_source(source, settings.max_file_bytes)
    units, tasks = {}, {}
    if set(artifact_plan.strategy_by_unit) != {u.id for u in artifact_plan.units}:
        raise AppError(
            "INVALID_ARTIFACT", "Artifact strategies do not cover the detected units.", 409
        )
    if set(probes) - artifact_plan.strategy_by_unit.keys():
        raise AppError("INVALID_ARTIFACT", "Probe refers to an unknown unit.", 409)
    for unit in artifact_plan.units:
        strategy = artifact_plan.strategy_by_unit[unit.id]
        if unit.framework == Framework.FASTAPI and strategy != "container":
            raise AppError("INVALID_ARTIFACT", "FastAPI requires its Stage 2 container plan.", 409)
        if strategy == "container":
            fields = _container_spec(source, artifact_plan, unit, probes.get(unit.id))
        else:
            fields = _node_spec(source, unit, strategy, settings, inventory)
            if strategy == "native":
                fields["probe"] = probes.get(unit.id) or ProbeSpec(port=3000)
                fields["port_from_env"] = True
        level = (
            "static_output_verified"
            if strategy == "static"
            else (
                "application_health_confirmed"
                if fields["probe"].health_contract
                else "http_responded"
            )
        )
        spec = UnitExecutionSpec(
            unit_id=unit.id,
            framework=unit.framework,
            strategy=strategy,
            root_path=unit.root_path,
            required_level=level,
            limitations=["Startup prerequisites were detected and were not executed."]
            if unit.runtime.prerequisite_commands
            else [],
            **fields,
        )
        units[unit.id] = spec
        prefix = "" if spec.context_path == "." else spec.context_path + "/"
        selected = spec.selected_paths
        inputs = {
            path: facts
            for path, facts in inventory.items()
            if path.startswith(prefix)
            and (selected is None or any(PurePosixPath(path).is_relative_to(p) for p in selected))
        }
        spec.source_inventory_digest = digest(inputs)
        initial = digest(
            {
                "inputs": inputs,
                "unit": spec.model_dump(mode="json"),
                "policy": execution_policy_fingerprint(settings),
            }
        )
        operations = (
            ["build", "static_output"] if strategy == "static" else ["build", "startup", "probe"]
        )
        previous = []
        for operation in operations:
            key = f"{unit.id}:{operation}"
            tasks[key] = ExecutionTask(
                task_id=key,
                unit_id=unit.id,
                operation=operation,
                depends_on=previous,
                input_digest=initial,
            )
            previous = [key]
    return ExecutionPlan(
        plan_id=uuid4().hex,
        artifact_plan_id=artifact_plan.plan_id,
        artifact_plan_digest=digest(artifact_plan.model_dump(mode="json")),
        units=units,
        tasks=tasks,
    )


# Create a private build copy for every app in the execution plan.
# Check that the candidate still matches the source inventory used when planning.
# If any copy fails, remove the copies already created before returning the error.
def freeze_plan_contexts(
    source: Path, plan: ExecutionPlan, settings: Settings
) -> dict[str, FrozenContext]:
    contexts = {}
    try:
        for unit_id, spec in plan.units.items():
            contexts[unit_id] = freeze_context(
                source,
                context=spec.context_path,
                selected=tuple(spec.selected_paths) if spec.selected_paths is not None else None,
                required=tuple(spec.required_paths),
                max_file_bytes=settings.max_file_bytes,
            )
            context = contexts[unit_id]
            captured = {
                path: {"hash": checksum, "mode": mode}
                for path, (_, checksum, mode) in zip(context.files, context.inventory, strict=True)
            }
            if spec.source_inventory_digest != digest(captured):
                raise AppError("context_changed", "Candidate inputs changed after planning.", 409)
        return contexts
    except BaseException:
        for context in contexts.values():
            remove_context(context)
        raise
