import ast
import difflib
import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

import yaml

from voxops.artifacts.ci_actions import APPROVED_ACTIONS
from voxops.artifacts.security import check_secret, redact, safe_read
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactPlan, ArtifactTask, ValidationResult


# Turns a list of check messages into a file check result.
# Marks the check failed when messages are present and passed when the list is empty.
def _result(name: str, messages: list[str], path: str, kind: str = "static") -> ValidationResult:
    return ValidationResult(
        validator_id=name,
        status="failed" if messages else "passed",
        severity="error" if messages else "info",
        kind=kind,
        messages=messages[:30],
        paths=[path] if path else [],
    )


# Checks whether ordered ignore rules exclude a given path.
# Applies matching rules to the path and its parent folders, including exceptions.
def _ignored(content: str, path: str) -> bool:
    ignored = False
    parts = PurePosixPath(path).parts
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for line in content.splitlines():
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        rule = rule.lstrip("!").strip("/")
        if any(
            fnmatch.fnmatchcase(candidate, rule)
            or ("/" not in rule and fnmatch.fnmatchcase(PurePosixPath(candidate).name, rule))
            for candidate in candidates
        ):
            ignored = not negate
    return ignored


# Checks Dockerfile instructions against the planned app and build inputs.
# Reports unsafe paths, missing commands, and other problems without running a build.
def _docker(task: ArtifactTask, content: str, plan: ArtifactPlan, root: Path | None) -> list[str]:
    errors = []
    lines = re.sub(r"\\\s*\n", " ", content).splitlines()
    instructions = [
        (m.group(1).upper(), m.group(2).strip())
        for line in lines
        if (m := re.match(r"^\s*([A-Za-z]+)\s+(.+?)\s*$", line))
    ]
    allowed = {
        "FROM",
        "RUN",
        "CMD",
        "ENTRYPOINT",
        "COPY",
        "ADD",
        "ARG",
        "ENV",
        "USER",
        "WORKDIR",
        "EXPOSE",
        "LABEL",
        "VOLUME",
        "SHELL",
        "STOPSIGNAL",
        "HEALTHCHECK",
        "ONBUILD",
        "MAINTAINER",
    }
    if any(
        not line.lstrip().startswith("#")
        and line.strip()
        and (
            not (match := re.match(r"^\s*([A-Za-z]+)\s+\S", line))
            or match.group(1).upper() not in allowed
        )
        for line in lines
    ):
        errors.append("Dockerfile contains an invalid instruction.")
    if (
        not instructions
        or instructions[0][0] not in {"ARG", "FROM"}
        or not any(op == "FROM" and arg for op, arg in instructions)
    ):
        errors.append("A valid FROM instruction is required.")
    last_from = max(
        (index for index, (op, _) in enumerate(instructions) if op == "FROM"), default=-1
    )
    final_stage = instructions[last_from + 1 :]
    users = [arg for op, arg in final_stage if op == "USER"]
    if not users or users[-1].split(":")[0] in {"root", "0"}:
        errors.append("The final image must specify a non-root USER.")
    if not any(op in {"CMD", "ENTRYPOINT"} for op, _ in final_stage):
        errors.append("A runtime CMD or ENTRYPOINT is required.")
    unit = next((unit for unit in plan.units if unit.id == task.unit_id), None)
    if unit and unit.runtime.start_command:
        runtime = " ".join(arg for op, arg in final_stage if op in {"CMD", "ENTRYPOINT"})
        if not _command_present(unit.runtime.start_command, runtime):
            errors.append("The runtime command does not match the discovered start command.")
    if unit and unit.runtime.build_command:
        builds = " ".join(arg for op, arg in instructions if op == "RUN")
        if not _command_present(unit.runtime.build_command, builds):
            errors.append("The discovered build command is missing from the Dockerfile.")
    for op, arg in instructions:
        if op not in {"COPY", "ADD"}:
            continue
        if "--from=" in arg:
            continue
        try:
            words = shlex.split(arg)
        except ValueError:
            errors.append("A COPY or ADD instruction cannot be parsed.")
            continue
        words = [word for word in words if not word.startswith("--")]
        if len(words) < 2:
            errors.append("A COPY or ADD instruction needs a source and destination.")
            continue
        for source in words[:-1]:
            source = source.strip('[],"')
            if (
                source.startswith(("/", "../", "http:", "https:"))
                or "/../" in source
                or ".." in PurePosixPath(task.build_context).parts
                or PurePosixPath(task.build_context).is_absolute()
            ):
                errors.append("COPY or ADD uses a source outside the build context.")
                continue
            if re.search(r"(^|/)(\.env[^/]*|\.git|[^/]+\.(?:pem|key))($|/)", source, re.I):
                errors.append("COPY or ADD includes a sensitive path.")
            if root and source not in {".", "./"} and not any(c in source for c in "*?$["):
                target = root / task.build_context / source
                components = (root / task.build_context / source).relative_to(root).parts
                if not target.exists() or any(
                    (root.joinpath(*components[:i])).is_symlink()
                    for i in range(1, len(components) + 1)
                ):
                    errors.append("A COPY source is absent or unsafe in the build context.")
    if re.search(
        r"(?im)^\s*(?:ENV|ARG)\s+[^\n]*(?:password|secret|token|api[_-]?key)\s*=", content
    ):
        errors.append("Docker instructions must not define credential values.")
    return errors


# Checks whether a Docker instruction contains the expected command arguments.
# Splits shell and JSON-style text into arguments without running the command.
def _command_present(expected: str, instruction: str) -> bool:
    try:
        wanted = shlex.split(expected)
        normalized = re.sub(r'[\[\],"]', " ", instruction)
        actual = shlex.split(normalized)
    except ValueError:
        return False
    return bool(wanted) and any(actual[i : i + len(wanted)] == wanted for i in range(len(actual)))


# Checks a GitHub Actions workflow against the plan and project files.
# Reports unsafe permissions, actions, commands, triggers, and missing build steps.
def _workflow(
    task: ArtifactTask, content: str, plan: ArtifactPlan, root: Path | None = None
) -> list[str]:
    errors = []
    try:
        data = yaml.load(content, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return ["Workflow YAML is invalid."]
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict) or not data["jobs"]:
        return ["Workflow must contain a jobs mapping."]
    triggers = data.get("on")
    if not triggers:
        errors.append("Workflow triggers are missing.")
    if "pull_request_target" in str(triggers):
        errors.append("The pull_request_target trigger requires manual redesign.")
    if re.search(r"\$\{\{\s*secrets\.", content, re.I):
        errors.append("Workflow must not access repository secrets.")
    if re.search(r"\$\{\{\s*github\.token\b", content, re.I):
        errors.append("Workflow must not expose the GitHub token through expressions.")
    permissions = data.get("permissions")
    if not isinstance(permissions, dict) or any(
        not isinstance(v, str) or v not in {"read", "none"} for v in permissions.values()
    ):
        errors.append("Workflow permissions must be explicitly read-only.")
    commands = []
    scripts = set()
    if root is not None:
        for unit in plan.units:
            if unit.language != "javascript":
                continue
            raw = safe_read(root, str(PurePosixPath(unit.root_path) / "package.json"))
            try:
                package = json.loads(raw) if raw else {}
                if isinstance(package.get("scripts"), dict):
                    scripts.update(package["scripts"])
            except (ValueError, AttributeError):
                errors.append("Package scripts could not be checked against the workflow.")
    for job in data["jobs"].values():
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            errors.append("Every job needs a steps list.")
            continue
        if any(key in job for key in ("uses", "services", "container")):
            errors.append("Reusable jobs, services, and job containers require manual review.")
        if not isinstance(job.get("runs-on"), str) or job["runs-on"] not in {
            "ubuntu-latest",
            "ubuntu-22.04",
            "ubuntu-24.04",
        }:
            errors.append("Job must use an approved hosted runner.")
        if "permissions" in job and (
            not isinstance(job["permissions"], dict)
            or any(
                not isinstance(v, str) or v not in {"read", "none"}
                for v in job["permissions"].values()
            )
        ):
            errors.append("Job permissions must be read-only.")
        for step in job["steps"]:
            if not isinstance(step, dict):
                errors.append("Workflow steps must be mappings.")
                continue
            action = step.get("uses", "")
            if not isinstance(action, str) or (
                action and action not in {f"{name}@{sha}" for name, sha in APPROVED_ACTIONS.items()}
            ):
                errors.append(
                    "Actions must use an approved official action pinned to a verified commit SHA."
                )
            run = step.get("run", "")
            if isinstance(action, str) and action.startswith("actions/checkout@"):
                options = step.get("with", {})
                if not isinstance(options, dict) or options.get("persist-credentials") != "false":
                    errors.append(
                        "Checkout must disable persist-credentials before project scripts run."
                    )
            if not isinstance(run, str):
                errors.append("Workflow shell commands must be text.")
                continue
            commands.append(run)
            if root is not None:
                invoked = re.findall(r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?([A-Za-z0-9:_-]+)", run)
                builtins = {"install", "ci", "exec", "dlx", "config", "version", "--version"}
                if any(script not in scripts and script not in builtins for script in invoked):
                    errors.append("Workflow invokes a package script absent from the repository.")
                if re.search(r"\bpytest\b", run):
                    bases = {root, *(root / unit.root_path for unit in plan.units)}
                    tests = [
                        path
                        for base in bases
                        for pattern in ("tests/test_*.py", "test_*.py")
                        for path in base.glob(pattern)
                    ]
                    if not tests:
                        errors.append("Workflow pytest command has no discovered test files.")
            if "${{" in run:
                errors.append("Workflow expressions in shell commands require manual redesign.")
            if re.search(r"\b(?:deploy|publish|push)\b", run, re.I):
                errors.append("Deployment or publishing commands are outside this stage.")
            if re.search(r"\b(?:curl|wget|aws|gcloud|az|kubectl|ssh|scp|nc|netcat)\b", run, re.I):
                errors.append(
                    "Workflow shell commands include an unapproved network or cloud tool."
                )
    for command in task.expected_commands:
        if command and not any(command in run for run in commands):
            errors.append("A planned build or test command is missing from the workflow.")
    return errors


# Checks that ignore rules cover common secret and Git paths.
# Also reports when the rules exclude a file needed for the planned build.
def _ignore(task: ArtifactTask, content: str) -> list[str]:
    errors = []
    for path in (".env", ".env.production", ".git/config", "private.pem", "private.key"):
        if not _ignored(content, path):
            errors.append(
                "Ignore rules do not protect all environment, key, and Git metadata paths."
            )
            break
    for path in task.required_paths:
        if _ignored(content, path):
            errors.append("Ignore rules exclude a required source or manifest path.")
            break
    return errors


# Compares a proposed source edit with the existing file.
# Checks added lines for unsafe URLs and browser-public secrets, and checks CORS changes.
# Rejects protected configuration edits and invalid Python or JSON syntax.
def _patch(task: ArtifactTask, content: str, root: Path | None) -> list[str]:
    if root is None:
        return ["Source patch validation requires the original repository."]
    before = safe_read(root, task.target_path)
    if before is None:
        return ["Source patch requires a readable existing file."]
    old = before.decode("utf-8", errors="replace")
    diff = difflib.ndiff(old.splitlines(), content.splitlines())
    added = "\n".join(line[2:] for line in diff if line.startswith("+ "))
    errors = []
    wildcard = re.compile(r"(?:allow_origins|cors|origin)\s*[:=][\s\S]{0,200}?[\"']\*[\"']", re.I)
    if wildcard.search(content) and not wildcard.search(old):
        errors.append("Source patch introduces wildcard CORS configuration.")
    if re.search(
        r"\b(?:VITE_|NEXT_PUBLIC_)[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE|CREDENTIAL)"
        r"[A-Z0-9_]*\b",
        added,
        re.I,
    ):
        errors.append("Source patch introduces a probable secret as browser-public configuration.")
    if task.target_path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError:
            errors.append("Python source patch has invalid syntax.")
    elif task.target_path.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError:
            errors.append("JSON source patch has invalid syntax.")
    if (
        re.search(r'https?://|(?:allow_origins|cors)[^\n]*["\']\*', added, re.I)
        or PurePosixPath(task.target_path).name
        in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock"}
        or ".env" in task.target_path
    ):
        errors.append(
            "Source patch adds an unsafe URL, CORS rule, or protected configuration change."
        )
    return errors


# Checks a requested file for secrets, size, and rules for its file type.
# Optionally runs actionlint for workflows and returns the check results and timing.
def validate_artifact(
    task: ArtifactTask,
    content: str,
    plan: ArtifactPlan,
    *,
    allow_tools: bool = False,
    root: Path | None = None,
) -> list[ValidationResult]:
    started = time.monotonic()
    errors = []
    try:
        check_secret(task.target_path, content.encode("utf-8"))
    except AppError:
        errors.append("Artifact contains sensitive or binary material.")
    if len(content.encode("utf-8")) > 1048576:
        errors.append("Artifact exceeds the size limit.")
    if task.artifact_type == "dockerfile":
        errors.extend(_docker(task, content, plan, root))
    elif task.artifact_type == "ci":
        errors.extend(_workflow(task, content, plan, root))
    elif task.artifact_type in {"dockerignore", "gitignore"}:
        errors.extend(_ignore(task, content))
    elif task.artifact_type == "source_patch":
        errors.extend(_patch(task, content, root))
    elif task.artifact_type == "provider" and not plan.provider:
        errors.append("Provider is unspecified; provider configuration cannot be validated.")
    results = [_result(task.artifact_type, errors, task.target_path)]
    if task.artifact_type == "dockerfile":
        results.append(
            ValidationResult(
                validator_id="docker_build_check",
                status="unavailable",
                severity="warning",
                kind="tool_check",
                messages=["Docker build checks were not run."],
            )
        )
    if allow_tools and task.artifact_type == "ci":
        executable = shutil.which("actionlint")
        if not executable:
            results.append(
                ValidationResult(
                    validator_id="actionlint",
                    status="unavailable",
                    severity="warning",
                    kind="tool_check",
                    messages=["actionlint is unavailable."],
                )
            )
        else:
            with tempfile.NamedTemporaryFile(suffix=".yml", mode="w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                try:
                    process = subprocess.run(
                        [executable, "-shellcheck=", "-pyflakes=", "-no-color", stream.name],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    results.append(
                        _result(
                            "actionlint",
                            ["actionlint reported workflow errors."] if process.returncode else [],
                            task.target_path,
                            "tool_check",
                        )
                    )
                    version = subprocess.run(
                        [executable, "-version"], capture_output=True, timeout=2, check=False
                    )
                    results[-1].tool_version = redact(
                        version.stdout.decode(errors="replace")[:100]
                    ).strip()
                except (OSError, subprocess.TimeoutExpired):
                    results.append(
                        ValidationResult(
                            validator_id="actionlint",
                            status="unavailable",
                            severity="warning",
                            kind="tool_check",
                            messages=["actionlint could not complete."],
                        )
                    )
    elapsed = int((time.monotonic() - started) * 1000)
    for result in results:
        result.duration_ms = elapsed
    return results


# Checks whether planned Dockerfiles, ignore rules, and workflows agree.
# Reports missing build inputs, unsafe context contents, and mismatched build paths.
def validate_cross_artifacts(
    plan: ArtifactPlan, contents: dict[str, str], root: Path | None = None
) -> list[ValidationResult]:
    errors = []
    for task in plan.tasks.values():
        if task.artifact_type != "dockerfile" or task.target_path not in contents:
            continue
        dockerfile = contents[task.target_path]
        for other in plan.tasks.values():
            if other.artifact_type == "dockerignore" and other.target_path in contents:
                ignore = contents[other.target_path]
                for required in task.required_paths:
                    if _ignored(ignore, required):
                        errors.append("Docker ignore rules exclude a planned build input.")
                if re.search(r"(?im)^\s*(?:COPY|ADD)\s+\.(?:/)?\s", dockerfile) and any(
                    not _ignored(ignore, path) for path in (".env", ".git/config", "private.key")
                ):
                    errors.append("Docker context copy can include sensitive files.")
            if other.artifact_type == "ci" and other.target_path in contents:
                workflow = contents[other.target_path]
                if (
                    re.search(r"\bdocker\s+build\b", workflow)
                    and task.target_path != "Dockerfile"
                    and task.target_path not in workflow
                ):
                    errors.append(
                        "Workflow Docker build does not reference the planned Dockerfile."
                    )
                for match in re.finditer(r"\bdocker\s+(?:buildx\s+)?build\s+([^\n]+)", workflow):
                    try:
                        arguments = shlex.split(match.group(1))
                    except ValueError:
                        errors.append("Workflow Docker build arguments cannot be parsed.")
                        continue
                    selected = "Dockerfile"
                    for index, argument in enumerate(arguments):
                        if argument in {"-f", "--file"} and index + 1 < len(arguments):
                            selected = arguments[index + 1]
                        elif argument.startswith("--file="):
                            selected = argument.partition("=")[2]
                    if selected == task.target_path and (
                        not arguments or arguments[-1] != task.build_context
                    ):
                        errors.append(
                            "Workflow Docker build context differs from the planned context."
                        )
    return [_result("cross_artifacts", errors, "", "cross_artifact")]
