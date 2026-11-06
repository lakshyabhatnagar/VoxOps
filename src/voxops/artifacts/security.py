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
    r'\b[A-Za-z0-9_-]*(?:api[_-]?key|secret|password|token|credential)[A-Za-z0-9_-]*'
    r'\s*["\']?\s*[:=]\s*["\']?'
    r"(?!\$|\{|<|\*|\[|(?:example|placeholder|changeme|none|null|false|true)(?:[\s\"',}]|$))"
    r"[A-Za-z0-9_./+\-=]{8,}",
    re.IGNORECASE,
)
_FORBIDDEN = {".env", ".npmrc", ".pypirc", ".netrc", ".git", ".ssh", ".aws", ".voxops"}
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


# Reads a file beneath the given folder, up to the requested size limit.
# Rejects unsafe paths and symbolic links, and returns nothing if the file is missing.
def safe_read(root: Path, relative: str, max_bytes: int = 131072) -> bytes | None:
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


# Checks a file path and its contents for likely credentials or protected files.
# Raises an error when it finds sensitive content or disallowed binary data.
def check_secret(path: str, data: bytes, *, allow_binary: bool = False) -> None:
    parts = PurePosixPath(path).parts
    if (
        any(
            p.lower() in _FORBIDDEN
            or p.lower().startswith(".env")
            or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            for p in parts
        )
        or (b"\x00" in data and not allow_binary)
    ):
        raise AppError("secret_detected", "Sensitive or binary content cannot be included.")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        if not allow_binary:
            raise AppError(
                "secret_detected", "Sensitive or binary content cannot be included."
            ) from None
        content = data.decode("latin-1")
    if _SECRET.search(content):
        raise AppError("secret_detected", "Probable credential material cannot be included.")


# Replaces likely credentials and private keys in text with a marker.
# Returns the remaining text for use in messages or AI requests.
def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", _PRIVATE_KEY.sub("[REDACTED]", text))
