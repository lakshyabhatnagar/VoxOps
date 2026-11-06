import json
import re
import tomllib
from pathlib import PurePosixPath

from voxops.domain.models import ManifestSummary
from voxops.repository.env_inspector import safe_command


# Extracts package names from dependency declarations.
# Drops version information, addresses and credentials.
def dependency_names(values: list[str]) -> list[str]:
    return sorted(
        {
            match.group(0).lower().replace("_", "-")
            for value in values
            if isinstance(value, str) and (match := re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value))
        }
    )


# Reads dependency information from a supported manifest.
# Parses the file without running its build scripts or package code.
def read_manifest(path: str, text: str) -> ManifestSummary | None:
    name = PurePosixPath(path).name
    root = str(PurePosixPath(path).parent)
    if name == "package.json":
        data = json.loads(text)
        dependencies = {}
        for field in ("dependencies", "devDependencies", "peerDependencies"):
            dependencies.update(data.get(field, {}))
        names = sorted(key for key in dependencies if re.fullmatch(r"[@\w./-]+", key))
        manager = str(data.get("packageManager", "")).split("@")[0]
        version = data.get("engines", {}).get("node")
        build = data.get("scripts", {}).get("build", "")
        build = build if isinstance(build, str) else ""
        output = re.search(r"--outDir(?:=|\s+)['\"]?([\w./-]+)(?:['\"]|\s|$)", build)
        return ManifestSummary(
            path=path,
            root_path=root,
            kind="node",
            dependencies=names,
            package_name=data.get("name")
            if isinstance(data.get("name"), str) and re.fullmatch(r"[@\w./-]+", data["name"])
            else None,
            scripts={
                key: safe_command(value)
                for key, value in data.get("scripts", {}).items()
                if key in {"build", "start", "dev", "preview"}
            },
            package_manager=manager if manager in {"npm", "pnpm", "yarn", "bun"} else None,
            runtime_version=version
            if isinstance(version, str) and re.fullmatch(r"[\d.><=^~| x*-]{1,40}", version)
            else None,
            workspace=bool(data.get("workspaces")),
            build_output_directory=output[1] if output else None,
            build_output_dynamic="--outDir" in build and not output,
            server_build=bool(re.search(r"(?:^|\s)--ssr(?:[=\s]|$)", build)),
            local_dependencies=[
                key
                for key in names
                if isinstance(dependencies[key], str)
                and dependencies[key].startswith(("workspace:", "file:", "link:"))
            ],
        )
    if name == "pyproject.toml":
        data = tomllib.loads(text)
        project = data.get("project", {})
        dependencies = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            dependencies.extend(group)
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        dependencies.extend(poetry)
        version = project.get("requires-python")
        return ManifestSummary(
            path=path,
            root_path=root,
            kind="python",
            dependencies=dependency_names(dependencies),
            runtime_version=version
            if isinstance(version, str) and re.fullmatch(r"[\d.><=, *!~]{1,40}", version)
            else None,
            workspace=bool(data.get("tool", {}).get("uv", {}).get("workspace")),
        )
    if re.fullmatch(r"requirements[^/]*\.txt", name):
        lines = [line for line in text.splitlines() if not line.lstrip().startswith(("#", "-"))]
        return ManifestSummary(
            path=path, root_path=root, kind="python", dependencies=dependency_names(lines)
        )
    return None
