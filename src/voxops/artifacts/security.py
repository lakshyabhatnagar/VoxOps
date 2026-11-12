import ast
import os
import re
import stat
from pathlib import Path, PurePosixPath

from voxops.core.errors import AppError

_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b|"
    r"\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://"
    r"[^\s:/\"']+:[^\s@/\"']+@[^\s\"']*|"
    r"\bBearer[ \t]+[A-Za-z0-9._~-]{16,}|"
    r"\b(?!VITE_|NEXT_PUBLIC_)[A-Za-z0-9_-]*"
    r"(?:api[_-]?key|secret|password|token|credential)[A-Za-z0-9_-]*"
    r'\s*["\']?\s*[:=]\s*'
    r'["\']'
    r"(?!\$|\{|<|\*|\[|(?:example|placeholder|changeme|none|null|false|true)(?:[\s\"',}]|$))"
    r"[A-Za-z0-9_./+\-=]{8,}[\"']",
    re.IGNORECASE,
)
_BARE_SECRET = re.compile(
    r"^[ \t]*(?!VITE_|NEXT_PUBLIC_)[A-Za-z0-9_-]*"
    r"(?:api[_-]?key|secret|password|token|credential)[A-Za-z0-9_-]*"
    r"[ \t]*[:=][ \t]*"
    r"(?!(?:example|placeholder|changeme|none|null|false|true)(?:[ \t;#]|$))"
    r"[A-Za-z0-9_./+\-=]{8,}(?=[ \t]*(?:[;#]|$))",
    re.IGNORECASE | re.MULTILINE,
)
_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

_FORBIDDEN = {".env", ".npmrc", ".pypirc", ".netrc", ".git", ".ssh", ".aws", ".voxops"}
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_PRIVATE_NAME = re.compile(r"api[_-]?key|secret|password|token|credential|private", re.I)
_LITERAL = (
    r'''(?:"""[\s\S]*?"""|'''
    + r"'''[\s\S]*?'''"
    + r"""|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)"""
)
_SOURCE_ASSIGNMENT = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\s*:\s*[^=\n;]{1,160})?\s*=\s*"
    rf"(?P<literal>{_LITERAL})"
)
_JS_FALLBACK = re.compile(
    r"\b(?:process\s*\.\s*env|import\s*\.\s*meta\s*\.\s*env)\s*"
    r"(?:\.\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)|"
    r"\[\s*['\"](?P<bracket_name>[A-Za-z_][A-Za-z0-9_]*)['\"]\s*\])"
    r"\s*(?:\?\?|\|\|)\s*"
    rf"(?P<literal>{_LITERAL})"
)


# Decide whether a configuration name looks private, such as a token or password.
# Exclude frontend names explicitly marked public by their supported prefixes.
# Other source checks use this result before deciding whether a literal value could expose a
# secret.
def _private_name(name: str) -> bool:
    return not name.startswith(("VITE_", "NEXT_PUBLIC_")) and bool(_PRIVATE_NAME.search(name))


# Check whether a private-looking name has a meaningful literal string value.
# Ignore supported obvious placeholders and non-string values.
# This helps distinguish an actual embedded credential from an example or ordinary default flag.
def _private_literal(name: str, value) -> bool:
    return (
        _private_name(name)
        and isinstance(value, str)
        and bool(value.strip())
        and not re.fullmatch(
            r"(?i)(?:example|placeholder|change[_ -]?me|replace[_ -]?me|none|null|false|true|"
            r"your[_ -].*|<[^>]+>|\$.*|\{.*|\*+|\[.*)",
            value.strip(),
        )
    )


# Inspect source text for supported hardcoded private assignments and fallback values.
# Use Python syntax and recognized settings aliases when possible, with limited text patterns as
# fallback.
# Return whether sensitive literal content was found without importing or executing the candidate
# code.
def _source_secret(text: str) -> bool:
    try:
        tree = ast.parse(text) if len(text) <= 262144 else None
    except (SyntaxError, ValueError, RecursionError):
        tree = None
    if tree is None:
        # Syntax errors elsewhere must not reopen recognizable literal assignments.
        for pattern in (_SOURCE_ASSIGNMENT, _JS_FALLBACK):
            for match in pattern.finditer(text):
                key = match["name"] or match.groupdict().get("bracket_name", "")
                if _private_literal(key, match["literal"].strip("\"'`")):
                    return True
        return False

    imports = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update((a.asname or a.name, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update((a.asname or a.name, f"{node.module}.{a.name}") for a in node.names)

    # Recover a dotted name from a supported Python name or attribute node.
    # Expand import aliases so a renamed import still matches the supported APIs.
    # Return an empty name for syntax this helper does not resolve.
    def name(node):
        if isinstance(node, ast.Name):
            return imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""

    # Extract a literal value from a supported Python expression.
    # For a recognized Field call, inspect its explicit or first positional default.
    # Return no value for expressions that would require evaluating application code.
    def default(node):
        if isinstance(node, ast.Call) and name(node.func) in {"Field", "pydantic.Field"}:
            node = next(
                (kw.value for kw in node.keywords if kw.arg == "default"),
                node.args[0] if node.args else None,
            )
        return node.value if isinstance(node, ast.Constant) else None

    classes, inherited, settings_fields = {}, set(), set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [name(base) for base in node.bases]
        if not any(
            base in {"BaseSettings", "pydantic_settings.BaseSettings"} or base in classes
            for base in bases
        ):
            continue
        prefix, fields = "", {}
        for base in bases:
            if base in classes:
                inherited.add(base)
                prefix = classes[base][0]
                fields.update(classes[base][1])
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "model_config"
                for target in stmt.targets
            ):
                config = stmt.value
                entries = (
                    [(kw.arg, kw.value) for kw in config.keywords]
                    if isinstance(config, ast.Call)
                    else [
                        (default(key), value)
                        for key, value in zip(config.keys, config.values, strict=True)
                    ]
                    if isinstance(config, ast.Dict)
                    else []
                )
                for key, value in entries:
                    if key == "env_prefix" and isinstance(default(value), str):
                        prefix = default(value)
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                settings_fields.add(stmt)
                alias = None
                if isinstance(stmt.value, ast.Call) and name(stmt.value.func) in {
                    "Field",
                    "pydantic.Field",
                }:
                    aliases = {kw.arg: default(kw.value) for kw in stmt.value.keywords}
                    alias = aliases.get("validation_alias") or aliases.get("alias")
                fields[stmt.target.id] = (alias, default(stmt.value))
        classes[node.name] = prefix, fields
    for class_name, (prefix, fields) in classes.items():
        if class_name not in inherited and any(
            _private_literal(alias if isinstance(alias, str) else prefix + key, value)
            for key, (alias, value) in fields.items()
        ):
            return True

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node not in settings_fields:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_private_literal(name(target), default(node.value)) for target in targets):
                return True
        if (
            isinstance(node, ast.Call)
            and name(node.func) in {"os.getenv", "os.environ.get", "getenv", "environ.get"}
            and node.args
        ):
            key = default(node.args[0])
            fallback = next(
                (kw.value for kw in node.keywords if kw.arg == "default"),
                node.args[1] if len(node.args) > 1 else None,
            )
            if isinstance(key, str) and _private_literal(key, default(fallback)):
                return True
    return False


# Read an allowed repository file through directory handles without following links.
# Reject protected paths, oversized data, and non-regular files.
# Return no content when the file is absent so callers can distinguish creation from modification.
def safe_read(root: Path, relative: str, max_bytes: int = 1048576) -> bytes | None:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or str(path) != relative
        or "\\" in relative
        or any(
            p in {".", ".."}
            or p.lower() in _FORBIDDEN
            or p.lower().startswith(".env")
            or (p.startswith(".") and p not in {".github", ".gitignore", ".dockerignore"})
            or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            for p in path.parts
        )
    ):
        raise AppError("unsafe_path", "The requested path is not allowed.")
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for component in path.parts[:-1]:
            if stat.S_ISLNK(os.stat(component, dir_fd=current, follow_symlinks=False).st_mode):
                raise AppError("unsafe_path", "Symbolic links cannot be read.")
            current = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            descriptors.append(current)
        if stat.S_ISLNK(os.stat(path.name, dir_fd=current, follow_symlinks=False).st_mode):
            raise AppError("unsafe_path", "Symbolic links cannot be read.")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AppError("unsafe_path", "Only regular files can be read.")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise AppError("file_too_large", "The requested file exceeds the size limit.")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AppError("unsafe_path", "The requested file could not be read safely.") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


# Reject protected filenames and likely credential material before content leaves the repository
# boundary.
# Check supported hardcoded source defaults as well as recognizable token and key patterns.
# Allow binary content only when the caller explicitly requests it; this remains a limited
# detector, not proof of no secrets.
def check_secret(path: str, data: bytes, *, allow_binary: bool = False) -> None:
    parts = PurePosixPath(path).parts
    if any(
        p.lower() in _FORBIDDEN
        or p.lower().startswith(".env")
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for p in parts
    ) or (b"\x00" in data and not allow_binary):
        raise AppError("secret_detected", "Sensitive or binary content cannot be included.")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        if not allow_binary:
            raise AppError(
                "secret_detected", "Sensitive or binary content cannot be included."
            ) from None
        content = data.decode("latin-1")
    if (
        _SECRET.search(content)
        or _source_secret(content)
        or (PurePosixPath(path).suffix not in _SOURCE_SUFFIXES and _BARE_SECRET.search(content))
    ):
        raise AppError("secret_detected", "Probable credential material cannot be included.")


# Prepare diagnostic or model-facing text by hiding recognizable private material.
# If supported source-secret detection matches, hide the whole text rather than just part of it.
# Otherwise replace recognized credentials and private keys with redaction markers.
def redact(text: str, *, path: str | None = None) -> str:
    if _source_secret(text):
        return "[REDACTED]"
    if path is None or PurePosixPath(path).suffix not in _SOURCE_SUFFIXES:
        text = _BARE_SECRET.sub("[REDACTED]", text)
    return _SECRET.sub("[REDACTED]", _PRIVATE_KEY.sub("[REDACTED]", text))
