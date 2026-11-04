import json
import re
import shlex
from pathlib import PurePosixPath

from voxops.domain.models import StartupCommand
from voxops.repository.env_inspector import (
    ASGI_COMMAND,
    MIGRATION_COMMAND,
    RUNNER,
    safe_command,
)

TARGET = re.compile(rf"^{RUNNER}(?:python3? -m )?(?:uvicorn|gunicorn) (\S+)")
STARTUP_FILES = {"Procfile", "README.md", "Dockerfile"}


# Why: Match documented commands to actual applications instead of unrelated README examples.
# How: Extract the already-sanitized server module and exported application name.
def startup_target(command: str) -> str | None:
    match = TARGET.match(command)
    return match[1] if match else None


# Why: Retain startup preparation without treating arbitrary shell commands as safe metadata.
# How: Accept only known migration steps followed by a sanitized startup command.
def parse_startup(path: str, text: str, prerequisites: list[str]) -> StartupCommand | None:
    segments = [part.strip() for part in text.split("&&")]
    before = [safe_command(part) for part in segments[:-1]]
    command = safe_command(segments[-1].removeprefix("exec "))
    if not all(MIGRATION_COMMAND.fullmatch(part) for part in before):
        return None
    server = re.match(ASGI_COMMAND + r"(?: |$)", command)
    python_web = PurePosixPath(path).name == "Procfile" and command.startswith(
        ("python ", "python3 ")
    )
    if not server and not python_web:
        return None
    return StartupCommand(
        path=path,
        command=command,
        prerequisite_commands=list(dict.fromkeys(prerequisites + before)),
    )


# Why: Read Docker's final runtime instruction without executing Docker or build commands.
# How: Restrict parsing to the final stage and combine literal exec-form ENTRYPOINT/CMD arguments.
def docker_startup(text: str) -> str | None:
    stage = re.split(r"(?im)^\s*FROM\s+", text.replace("\\\n", " "))[-1]
    commands = re.findall(r"(?im)^\s*CMD\s+(.+)$", stage)
    entries = re.findall(r"(?im)^\s*ENTRYPOINT\s+(.+)$", stage)
    if not commands:
        return None
    command = commands[-1].strip()
    try:
        arguments = json.loads(command) if command.startswith("[") else None
        entrypoint = json.loads(entries[-1]) if entries else []
    except (ValueError, TypeError):
        return None
    if not isinstance(entrypoint, list) or any(not isinstance(item, str) for item in entrypoint):
        return None
    if arguments is None:
        return command if not entrypoint else None
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        return None
    if (
        not entrypoint
        and len(arguments) == 3
        and arguments[0] in {"sh", "/bin/sh", "bash", "/bin/bash"}
        and arguments[1] == "-c"
    ):
        return arguments[2]
    if any("$" in item for item in entrypoint + arguments):
        return None  # Exec-form Docker arguments do not expand environment variables.
    return shlex.join(entrypoint + arguments)


# Why: Discover existing startup instructions while retaining only allowlisted command metadata.
# How: Read Procfile web entries, fenced README shell blocks and final Docker CMD instructions.
def read_startup(path: str, text: str) -> list[StartupCommand]:
    name = PurePosixPath(path).name
    if name == "Dockerfile":
        command = docker_startup(text)
        candidate = parse_startup(path, command, []) if command else None
        return [candidate] if candidate else []
    if name == "Procfile":
        lines = [line[4:].strip() for line in text.splitlines() if line.startswith("web:")]
        blocks = ["\n".join(lines)]
    else:
        blocks = re.findall(r"(?ms)^```(?:bash|sh|shell|console)?[ \t]*\n(.*?)^```[ \t]*$", text)
    results = []
    for block in blocks:
        prerequisites: list[str] = []
        for line in block.splitlines():
            line = line.strip().removeprefix("$ ")
            if re.match(r"(?:cd|pushd|popd)\b", line):
                break  # A changed working directory needs explicit build-context handling.
            clean = safe_command(line)
            if MIGRATION_COMMAND.fullmatch(clean):
                prerequisites.append(clean)
                continue
            candidate = parse_startup(path, line, prerequisites)
            if candidate:
                results.append(candidate)
    return results
