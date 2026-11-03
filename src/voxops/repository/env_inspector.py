import re

from voxops.domain.models import ConfigRequirement

ENV_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)
ENV_REFERENCE = re.compile(
    r"(?:process\.env\.|import\.meta\.env\.)([A-Za-z_][A-Za-z0-9_]*)"
    r"|(?:os\.getenv|os\.environ\.get)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
    r"|(?:os\.environ|process\.env)\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
)


# Why: Extract configuration requirements without retaining values or source excerpts.
# How: Match assignment/reference identifiers and annotate presence and browser exposure.
def config_names(
    text: str, *, example: bool = False, env_file: bool = False
) -> list[ConfigRequirement]:
    names = (
        set(ENV_ASSIGN.findall(text))
        if env_file
        else {next(group for group in match if group) for match in ENV_REFERENCE.findall(text)}
    )
    result = []
    for name in sorted(names):
        public = name.startswith(("VITE_", "NEXT_PUBLIC_"))
        secret = bool(re.search(r"SECRET|TOKEN|PASSWORD|PRIVATE|API_KEY|DATABASE_URL", name))
        result.append(
            ConfigRequirement(
                name=name,
                stage="build_time" if public else "unknown",
                visibility="public" if public else "secret" if secret else "unknown",
                presence="example_only" if example else "present" if env_file else "referenced",
            )
        )
    return result


# Why: Preserve recognizable commands without returning embedded credentials or literal arguments.
# How: Keep only known tool invocations and safe module/script paths, redacting everything else.
def safe_command(command: str) -> str:
    if not isinstance(command, str) or len(command) > 1000:
        return "[redacted command]"
    allowed = re.fullmatch(
        r"(?:vite(?: build| preview)?|next(?: build| start| dev)?|"
        r"(?:uvicorn|gunicorn) [\w./:-]+(?: (?:--[\w-]+|[0-9]+|0\.0\.0\.0))*|"
        r"(?:node|python|python3) [\w./-]+|react-scripts (?:build|start))",
        command.strip(),
    )
    return command.strip() if allowed else "[redacted command]"
