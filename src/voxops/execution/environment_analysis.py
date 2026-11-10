"""Bounded static environment discovery without importing candidate code."""

import ast
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.domain.execution_models import EnvConsumer, EnvEvidence, EnvFormSchema, EnvRequirement
from voxops.domain.models import DeployableUnit
from voxops.storage.artifact_store import digest

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN = re.compile(
    r"\s+|//[^\n]*|/\*[\s\S]*?\*/|(?:'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"|`[^`\\]*(?:\\.[^`\\]*)*`)|[A-Za-z_$][\w$]*|\?\?|\|\||===|!==|==|!=|[^\s]",
    re.M,
)


def _annotation_type(node):
    if isinstance(node, ast.Name):
        return {"int": "integer", "bool": "boolean"}.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _annotation_type(node.left), _annotation_type(node.right)
        return left or right
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Optional"
    ):
        return _annotation_type(node.slice)
    return None


def _read(root: Path, relative: str, limit: int) -> str:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
        raise ValueError
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(data) > limit or (info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        return data.decode("utf-8")
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class Analyzer:
    def __init__(self, root: Path, units: list[DeployableUnit], source_digest: str):
        self.root, self.units, self.source_digest = root, units, source_digest
        self.requirements = {}
        self.defaults = {}
        self.extra_defaults = set()
        self.ambiguities = []
        self.env_files = {}

    def ambiguous(self, path: str, line: int | None, kind: str, detail: str) -> None:
        self.ambiguities.append(EnvEvidence(path=path, line=line, kind=kind, detail=detail))

    def add(
        self,
        unit: DeployableUnit,
        path: str,
        line: int | None,
        name: str,
        *,
        phase: str,
        necessity: str,
        default=None,
        has_default=False,
        kind="python",
        detail="Environment reference",
        declared_type=None,
        case_sensitive=True,
    ):
        if not NAME.fullmatch(name):
            self.ambiguous(path, line, kind, "Environment name is dynamic or invalid")
            return
        visibility = (
            "public"
            if name.startswith(("VITE_", "NEXT_PUBLIC_"))
            else "secret"
            if re.search(r"SECRET|TOKEN|PASSWORD|PRIVATE|API_KEY|DATABASE_URL", name)
            else "unknown"
        )
        scope = unit.root_path
        field_id = digest(
            {
                "scope": scope,
                "name": name,
                "phase": phase,
                "visibility": visibility,
                "case_sensitive": case_sensitive,
            }
        )[:32]
        evidence = EnvEvidence(path=path, line=line, kind=kind, detail=detail)
        existing = self.requirements.get(field_id)
        default_kind = ("none" if default is None else "literal") if has_default else "absent"
        value_type = (
            "port"
            if name == "PORT" or name.endswith("_PORT")
            else "url"
            if name.endswith("_URL")
            else declared_type or "string"
        )
        if existing:
            consumer = EnvConsumer(unit_id=unit.id, phase=phase)
            if consumer not in existing.consumers:
                existing.consumers.append(consumer)
            if necessity == "required":
                existing.necessity = "required"
                if not has_default:
                    existing.default_kind = "absent"
                    existing.resolution = "missing"
                    self.defaults.pop(field_id, None)
            if (
                has_default
                and existing.default_kind in {"literal", "none"}
                and (
                    existing.default_kind != default_kind
                    or (default is not None and self.defaults.get(field_id) != str(default))
                )
            ):
                self.ambiguous(path, line, kind, "Environment defaults conflict")
                if (
                    default is not None
                    and visibility != "public"
                    and value_type not in {"port", "integer", "boolean"}
                ):
                    self.extra_defaults.add(str(default))
            if declared_type and existing.value_type not in {"string", value_type}:
                self.ambiguous(path, line, kind, "Environment value types conflict")
            if evidence not in existing.evidence and len(existing.evidence) < 30:
                existing.evidence.append(evidence)
            return
        self.requirements[field_id] = EnvRequirement(
            field_id=field_id,
            name=name,
            consumers=[EnvConsumer(unit_id=unit.id, phase=phase)],
            scope=scope,
            case_sensitive=case_sensitive,
            necessity=necessity,
            default_kind=default_kind,
            visibility=visibility,
            resolution="default" if has_default else "missing",
            value_type=value_type,
            evidence=[evidence],
        )
        if has_default and default is not None:
            self.defaults[field_id] = str(default)

    def python(self, unit: DeployableUnit, path: str, text: str):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            self.ambiguous(path, None, "python", "Python source could not be analyzed")
            return
        settings_names, field_names = set(), set()
        os_names, environ_names, getenv_names = set(), set(), set()
        settings_modules = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        os_names.add(alias.asname or alias.name)
                    if alias.name == "pydantic_settings":
                        settings_modules.add(alias.asname or alias.name)
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        environ_names.add(alias.asname or alias.name)
                    if alias.name == "getenv":
                        getenv_names.add(alias.asname or alias.name)
            if isinstance(node, ast.ImportFrom) and node.module in {
                "pydantic_settings",
                "pydantic",
            }:
                for alias in node.names:
                    if alias.name == "BaseSettings" and node.module == "pydantic_settings":
                        settings_names.add(alias.asname or alias.name)
                    if alias.name == "Field" and node.module == "pydantic":
                        field_names.add(alias.asname or alias.name)
        classes = {}
        inherited = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if any(
                isinstance(b, ast.Attribute)
                and isinstance(b.value, ast.Name)
                and b.value.id in settings_modules
                and b.attr == "BaseSettings"
                for b in node.bases
            ):
                bases.append("__settings_module__")
                settings_names.add("__settings_module__")
            if not any(b in settings_names or b in classes for b in bases):
                if any(base.endswith("Settings") for base in bases):
                    self.ambiguous(
                        path, node.lineno, "python", "Settings inheritance is unresolved"
                    )
                continue
            fields = {}
            prefix, case_sensitive = "", False
            for base in bases:
                if base in classes:
                    inherited.add(base)
                    fields.update(classes[base][0])
                    prefix, case_sensitive = classes[base][1:]
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
                ):
                    config = stmt.value
                    if isinstance(config, ast.Call):
                        keywords = config.keywords
                    elif isinstance(config, ast.Dict) and all(
                        isinstance(key, ast.Constant) and isinstance(key.value, str)
                        for key in config.keys
                    ):
                        keywords = [
                            ast.keyword(arg=key.value, value=value)
                            for key, value in zip(config.keys, config.values, strict=True)
                        ]
                    else:
                        keywords = []
                        prefix = None
                        self.ambiguous(
                            path, stmt.lineno, "python", "Settings configuration is dynamic"
                        )
                    if keywords:
                        for kw in keywords:
                            if kw.arg is None:
                                prefix = None
                                self.ambiguous(
                                    path, stmt.lineno, "python", "Settings configuration is dynamic"
                                )
                            if kw.arg == "env_prefix":
                                if isinstance(kw.value, ast.Constant) and isinstance(
                                    kw.value.value, str
                                ):
                                    prefix = kw.value.value
                                else:
                                    prefix = None
                                    self.ambiguous(
                                        path, stmt.lineno, "python", "Settings prefix is dynamic"
                                    )
                            if kw.arg == "env_file":
                                nodes = (
                                    kw.value.elts
                                    if isinstance(kw.value, (ast.List, ast.Tuple))
                                    else [kw.value]
                                )
                                filenames = [
                                    item.value if isinstance(item, ast.Constant) else None
                                    for item in nodes
                                ]
                                if filenames and all(
                                    filename
                                    in {
                                        ".env",
                                        ".env.local",
                                        ".env.production",
                                        ".env.production.local",
                                    }
                                    for filename in filenames
                                ):
                                    self.env_files.setdefault(unit.root_path, []).extend(filenames)
                                else:
                                    self.ambiguous(
                                        path,
                                        stmt.lineno,
                                        "python",
                                        "Settings env file is not recognized",
                                    )
                            if kw.arg == "case_sensitive":
                                if isinstance(kw.value, ast.Constant) and isinstance(
                                    kw.value.value, bool
                                ):
                                    case_sensitive = kw.value.value
                                else:
                                    case_sensitive = None
                                    self.ambiguous(
                                        path,
                                        stmt.lineno,
                                        "python",
                                        "Settings case sensitivity is dynamic",
                                    )
                    continue
                if (
                    not isinstance(stmt, ast.AnnAssign)
                    or not isinstance(stmt.target, ast.Name)
                    or stmt.target.id.startswith("_")
                ):
                    continue
                value, alias, dynamic_alias = stmt.value, None, False
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in field_names
                ):
                    options = {kw.arg: kw.value for kw in value.keywords}
                    for name in ("validation_alias", "alias"):
                        alias_value = options.get(name)
                        if alias_value is None or (
                            isinstance(alias_value, ast.Constant) and alias_value.value is None
                        ):
                            continue
                        if isinstance(alias_value, ast.Constant) and isinstance(
                            alias_value.value, str
                        ):
                            alias = alias_value.value
                        else:
                            dynamic_alias = True
                            self.ambiguous(path, stmt.lineno, "python", "Settings alias is dynamic")
                        break
                    if "default_factory" in options:
                        self.ambiguous(
                            path, stmt.lineno, "python", "Settings default factory is dynamic"
                        )
                    value = options.get("default", value.args[0] if value.args else None)
                has_default = isinstance(value, ast.Constant) and value.value is not Ellipsis
                if value is not None and not isinstance(value, ast.Constant):
                    self.ambiguous(path, stmt.lineno, "python", "Settings default is dynamic")
                fields[stmt.target.id] = (
                    alias,
                    dynamic_alias,
                    has_default,
                    value.value if has_default else None,
                    stmt.lineno,
                    _annotation_type(stmt.annotation),
                )
            classes[node.name] = (fields, prefix, case_sensitive)
        for class_name, (fields, prefix, case_sensitive) in classes.items():
            if class_name in inherited:
                continue
            for key, (
                alias,
                dynamic_alias,
                has_default,
                default,
                line,
                value_type,
            ) in fields.items():
                if dynamic_alias or case_sensitive is None or (prefix is None and alias is None):
                    continue
                self.add(
                    unit,
                    path,
                    line,
                    alias or prefix + key,
                    phase="runtime",
                    necessity="optional" if has_default else "required",
                    default=default,
                    has_default=has_default,
                    detail="Settings field",
                    declared_type=value_type,
                    case_sensitive=case_sensitive,
                )

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.depth = 0
                self.conditional = 0

            def visit_FunctionDef(self, node):
                self.depth += 1
                self.generic_visit(node)
                self.depth -= 1

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_If(self, node):
                self.conditional += 1
                self.generic_visit(node)
                self.conditional -= 1

            def visit_Subscript(self, node):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id in environ_names
                    or isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id in os_names
                    and node.value.attr == "environ"
                ):
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        self_outer.add(
                            unit,
                            path,
                            node.lineno,
                            node.slice.value,
                            phase="runtime",
                            necessity="required"
                            if not self.depth and not self.conditional
                            else "optional",
                        )
                    elif not self.depth:
                        self_outer.ambiguous(
                            path, node.lineno, "python", "Environment key is dynamic"
                        )
                self.generic_visit(node)

            def visit_Call(self, node):
                func = node.func
                is_get = (
                    isinstance(func, ast.Name)
                    and func.id in getenv_names
                    or isinstance(func, ast.Attribute)
                    and (
                        (
                            isinstance(func.value, ast.Name)
                            and func.value.id in os_names
                            and func.attr == "getenv"
                        )
                        or (
                            isinstance(func.value, ast.Name)
                            and func.value.id in environ_names
                            and func.attr == "get"
                        )
                        or (
                            isinstance(func.value, ast.Attribute)
                            and isinstance(func.value.value, ast.Name)
                            and func.value.value.id in os_names
                            and func.value.attr == "environ"
                            and func.attr == "get"
                        )
                    )
                )
                if is_get:
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        default = node.args[1] if len(node.args) > 1 else None
                        literal = isinstance(default, ast.Constant)
                        if default is not None and not literal:
                            self_outer.ambiguous(
                                path, node.lineno, "python", "Environment fallback is dynamic"
                            )
                        self_outer.add(
                            unit,
                            path,
                            node.lineno,
                            node.args[0].value,
                            phase="runtime",
                            necessity="optional",
                            has_default=literal,
                            default=default.value if literal else None,
                        )
                    elif not self.depth:
                        self_outer.ambiguous(
                            path, node.lineno, "python", "Environment key is dynamic"
                        )
                self.generic_visit(node)

        self_outer = self
        Visitor().visit(tree)

    def javascript(self, unit: DeployableUnit, path: str, text: str):
        tokens = [
            m.group()
            for m in TOKEN.finditer(text)
            if not m.group().isspace() and not m.group().startswith(("//", "/*"))
        ]
        if len(tokens) > 200000:
            self.ambiguous(path, None, "javascript", "JavaScript token limit exceeded")
            return
        guards = set()
        depth = 0
        for i, token in enumerate(tokens):
            if token == "{":
                depth += 1
            elif token == "}":
                depth = max(0, depth - 1)
            if depth == 0 and tokens[i : i + 3] == ["if", "(", "!"]:
                try:
                    close = tokens.index(")", i + 3)
                except ValueError:
                    continue
                tail = tokens[close + 1 : close + 4]
                if tail[:1] == ["throw"] or tail[:2] == ["{", "throw"]:
                    guards.add(tuple(tokens[i + 3 : close]))
        for i in range(len(tokens)):
            offset = 0
            if tokens[i : i + 4] == ["process", ".", "env", "."]:
                offset = 4
            elif tokens[i : i + 6] == ["import", ".", "meta", ".", "env", "."]:
                offset = 6
            elif tokens[i : i + 4] == ["process", ".", "env", "["] or tokens[i : i + 6] == [
                "import",
                ".",
                "meta",
                ".",
                "env",
                "[",
            ]:
                self.ambiguous(path, None, "javascript", "Computed environment key requires review")
            if not offset or i + offset >= len(tokens):
                continue
            name = tokens[i + offset]
            if (offset == 6 and name in {"DEV", "PROD", "MODE", "BASE_URL", "SSR"}) or (
                offset == 4 and name == "NODE_ENV"
            ):
                continue
            fallback = tokens[i + offset + 1 : i + offset + 3]
            literal = (
                len(fallback) == 2
                and fallback[0] in {"||", "??"}
                and (
                    fallback[1].startswith(("'", '"'))
                    or fallback[1] in {"true", "false"}
                    or fallback[1].isdigit()
                )
            )
            access = tuple(tokens[i : i + offset + 1])
            assigned = (tokens[i - 2],) if i >= 2 and tokens[i - 1] == "=" else ()
            required = access in guards or assigned in guards
            phase = (
                "build"
                if name.startswith(("VITE_", "NEXT_PUBLIC_"))
                or unit.role.value == "static_frontend"
                else "runtime"
            )
            self.add(
                unit,
                path,
                None,
                name,
                phase=phase,
                necessity="required" if required else "optional",
                has_default=literal,
                default=fallback[1][1:-1]
                if literal and fallback[1].startswith(("'", '"'))
                else fallback[1]
                if literal
                else None,
                kind="javascript",
            )

    def scan(self):
        count = total = 0
        metadata = []
        for unit in self.units:
            if unit.framework.value == "react_vite":
                self.env_files.setdefault(unit.root_path, []).extend(
                    [".env", ".env.local", ".env.production", ".env.production.local"]
                )
            elif unit.framework.value == "nextjs":
                self.env_files.setdefault(unit.root_path, []).extend(
                    [".env", ".env.production", ".env.local", ".env.production.local"]
                )
            base = self.root / unit.root_path
            if (
                not base.is_dir()
                or base.is_symlink()
                or not base.resolve().is_relative_to(self.root.resolve())
            ):
                raise AppError("unsafe_env_source", "Unit source is unavailable or unsafe.", 409)
            for folder, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if d
                    not in {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
                    and not d.startswith(".")
                    and not any(
                        (Path(folder) / d).relative_to(self.root).as_posix() == other.root_path
                        for other in self.units
                        if other.id != unit.id and other.root_path != unit.root_path
                    )
                    and not (Path(folder) / d).is_symlink()
                )
                for filename in sorted(files):
                    path = (Path(folder) / filename).relative_to(self.root).as_posix()
                    if not (
                        filename.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))
                        or filename in {".env.example", ".env.sample"}
                    ):
                        continue
                    count += 1
                    if count > 3000:
                        raise AppError(
                            "env_scan_limit", "Environment scan file limit exceeded.", 409
                        )
                    try:
                        content = _read(self.root, path, 262144)
                    except (OSError, ValueError, UnicodeError):
                        self.ambiguous(
                            path,
                            None,
                            "example"
                            if filename.startswith(".env")
                            else "python"
                            if filename.endswith(".py")
                            else "javascript",
                            "Source file could not be safely analyzed",
                        )
                        continue
                    total += len(content.encode())
                    if total > 10_000_000:
                        raise AppError(
                            "env_scan_limit", "Environment scan byte limit exceeded.", 409
                        )
                    if filename.startswith(".env"):
                        for line, entry in enumerate(content.splitlines(), 1):
                            match = re.match(
                                r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", entry
                            )
                            if match:
                                metadata.append(
                                    (
                                        unit,
                                        path,
                                        line,
                                        match.group(1),
                                        "runtime",
                                        "example",
                                        "Example key",
                                    )
                                )
                    elif filename.endswith(".py"):
                        self.python(unit, path, content)
                    else:
                        self.javascript(unit, path, content)
            if unit.language == "python":
                self.env_files.setdefault(unit.root_path, [".env"])
            for item in unit.config_requirements:
                phase = "build" if item.stage == "build_time" else "runtime"
                metadata.append(
                    (unit, unit.root_path, None, item.name, phase, "stage1", "Stage 1 candidate")
                )
        source_fields = {}
        for item in self.requirements.values():
            for consumer in item.consumers:
                key = (item.scope, consumer.unit_id, consumer.phase, item.name.lower())
                source_fields.setdefault(key, []).append(item)
        for unit, path, line, name, phase, kind, detail in metadata:
            matches = [
                item
                for item in source_fields.get((unit.root_path, unit.id, phase, name.lower()), [])
                if item.name == name or not item.case_sensitive
            ]
            if matches:
                evidence = EnvEvidence(path=path, line=line, kind=kind, detail=detail)
                for item in matches:
                    if evidence not in item.evidence and len(item.evidence) < 30:
                        item.evidence.append(evidence)
                continue
            self.add(
                unit,
                path,
                line,
                name,
                phase=phase,
                necessity="optional",
                kind=kind,
                detail=detail,
            )
        schema = EnvFormSchema(
            source_digest=self.source_digest,
            scan_id=uuid4().hex,
            requirements=sorted(self.requirements.values(), key=lambda r: r.field_id),
            ambiguities=self.ambiguities,
            env_files={
                scope: list(dict.fromkeys(files)) for scope, files in self.env_files.items()
            },
        )
        return schema, self.defaults
