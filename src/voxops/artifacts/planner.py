from pathlib import Path, PurePosixPath
from uuid import uuid4

from voxops.artifacts.security import check_secret, safe_read
from voxops.core.errors import AppError
from voxops.domain.artifact_models import (
    ArtifactPlan,
    ArtifactTask,
    SourcePatchSpec,
    Strategy,
    artifact_path,
)
from voxops.domain.models import AnalysisResult, Framework, Role
from voxops.storage.artifact_store import digest


# Creates a file job from the supplied fields and checks its destination.
# Marks an existing file for checking and records a checksum of its contents.
def planned_task(root: Path, **fields) -> ArtifactTask:
    target = fields["target_path"]
    content = safe_read(root, target)
    return ArtifactTask(
        **fields,
        operation="validate" if content is not None else "create",
        existing_hash=digest(content) if content is not None else None,
    )


# Builds file jobs from the detected apps, chosen strategies, and requested source edits.
# Adds required files and job dependencies, and records questions that need answers.
def build_plan(
    root: Path,
    analysis: AnalysisResult,
    strategies: dict[str, Strategy],
    provider: str | None = None,
    patches: list[SourcePatchSpec] | None = None,
) -> ArtifactPlan:
    units = analysis.detected_units
    ids = {unit.id for unit in units}
    if not units or set(strategies) != ids:
        raise AppError("strategy_required", "Choose a strategy for every detected unit.", 409)
    questions = list(analysis.unresolved_ambiguities)
    if any(
        any(word in warning.lower() for word in ("truncat", "limit", "incomplete"))
        for warning in analysis.scan.warnings
    ):
        questions.append("The scan is incomplete; narrow the repository or increase scan limits.")
    tasks: dict[str, ArtifactTask] = {}
    containers = []
    for unit in units:
        strategy = strategies[unit.id]
        if (
            unit.framework == Framework.FASTAPI
            and strategy != "container"
            or unit.framework == Framework.VITE
            and strategy == "native"
            or unit.framework == Framework.NEXT
            and strategy == "static"
            and unit.role != Role.STATIC
        ):
            raise AppError("unsupported_strategy", "Strategy conflicts with detected runtime.", 409)
        questions.extend(unit.detection.unresolved_questions)
        if strategy == "static" and not unit.runtime.build_command:
            questions.append(f"Unit {unit.id} has no known static build command.")
        if strategy != "container":
            continue
        if not unit.runtime.start_command and unit.framework != Framework.VITE:
            questions.append(f"Unit {unit.id} has no known start command.")
        path = str(PurePosixPath(unit.root_path) / "Dockerfile")
        artifact_path(path)
        required = sorted(
            set(
                unit.runtime.dependency_files
                + unit.shared_packages
                + [
                    item.path
                    for item in unit.detection.evidence
                    if item.kind in {"fastapi_instance", "fastapi_factory", "config", "source"}
                ]
                + [
                    path
                    for path in set(analysis.scan.relevant_files + analysis.scan.shallow_tree)
                    if (
                        PurePosixPath(path).suffix
                        in {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css"}
                        and any(
                            PurePosixPath(path).is_relative_to(PurePosixPath(base))
                            for base in [unit.root_path, *unit.shared_packages]
                        )
                    )
                ]
                + ([unit.runtime.lockfile_path] if unit.runtime.lockfile_path else [])
            )
        )
        task = planned_task(
            root,
            task_id=f"docker-{unit.id}",
            unit_id=unit.id,
            artifact_type="dockerfile",
            target_path=path,
            strategy=strategy,
            build_context=".",
            required_paths=required,
            expected_commands=[
                command
                for command in [unit.runtime.build_command, unit.runtime.start_command]
                if command
            ],
            reason="Container strategy requires an explicit build and runtime definition.",
            evidence=[item.path for item in unit.detection.evidence],
        )
        tasks[task.task_id] = task
        containers.append(task.task_id)
    if containers:
        required = sorted({path for task in tasks.values() for path in task.required_paths})
        tasks["dockerignore"] = planned_task(
            root,
            task_id="dockerignore",
            artifact_type="dockerignore",
            target_path=".dockerignore",
            build_context=".",
            required_paths=required,
            reason="A single root build context includes inherited locks and shared packages.",
        )
    tasks["gitignore"] = planned_task(
        root,
        task_id="gitignore",
        artifact_type="gitignore",
        target_path=".gitignore",
        required_paths=sorted(
            set(
                path
                for unit in units
                for path in unit.runtime.dependency_files
                + unit.shared_packages
                + ([unit.runtime.lockfile_path] if unit.runtime.lockfile_path else [])
            )
            | {
                path
                for path in set(analysis.scan.relevant_files + analysis.scan.shallow_tree)
                if PurePosixPath(path).suffix
                in {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css"}
            }
        ),
        reason="Exclude local credentials and generated dependencies from publication.",
    )
    tasks["ci"] = planned_task(
        root,
        task_id="ci",
        artifact_type="ci",
        target_path=".github/workflows/ci.yml",
        depends_on=containers + (["dockerignore"] if containers else []),
        required_paths=[
            task.target_path for task in tasks.values() if task.artifact_type == "dockerfile"
        ],
        expected_commands=[
            command for unit in units for command in [unit.runtime.build_command] if command
        ],
        reason="One build/test workflow covers the detected units without cloud deployment.",
        evidence=[manifest.path for manifest in analysis.scan.manifests],
    )
    tasks["provider"] = ArtifactTask(
        task_id="provider",
        artifact_type="provider",
        target_path="voxops-provider.json",
        operation="skip",
        reason="No provider target selected; no provider configuration needed.",
    )
    if provider:
        questions.append(
            "Provider configuration requires a verified schema; this target is unsupported."
        )
        tasks["provider"].reason = "Selected provider has no verified Stage 2 schema."
    for index, patch in enumerate(patches or []):
        path = PurePosixPath(patch.target_path)
        if path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx", ".json"} or (
            "lock" in path.name.lower()
        ):
            raise AppError(
                "unsafe_patch", "Source patches require an approved source/config file.", 409
            )
        check_secret(patch.target_path, (patch.reason + patch.correction).encode())
        original = safe_read(root, patch.target_path)
        if original is None or digest(original) != patch.expected_hash:
            raise AppError("source_changed", "Source patch hash does not match its evidence.", 409)
        task = planned_task(
            root,
            task_id=f"patch-{index}",
            artifact_type="source_patch",
            target_path=patch.target_path,
            approval_class="source_patch",
            reason=patch.reason,
            evidence=[patch.correction],
        )
        task.operation = "modify"
        if any(existing.target_path == task.target_path for existing in tasks.values()):
            raise AppError("task_path_conflict", "Two tasks cannot own the same source file.", 409)
        tasks[task.task_id] = task
    return ArtifactPlan(
        plan_id=uuid4().hex,
        scan_id=analysis.scan.id,
        repo_fingerprint=analysis.scan.repo_fingerprint,
        source_input_fingerprint=analysis.scan.input_fingerprint,
        strategy_by_unit=strategies,
        units=units,
        tasks=tasks,
        warnings=["Static validation does not prove a build, running app, or GitHub CI success."],
        unresolved_questions=sorted(set(questions)),
        provider=provider,
    )
