import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from voxops.analysis.topology_resolver import TopologyResolver
from voxops.core.errors import AppError, ErrorDetail
from voxops.core.logging import event
from voxops.core.settings import Settings
from voxops.domain.models import AIUsage, AnalysisResult, DeployableUnit, RepoScan
from voxops.frameworks.registry import detect_units
from voxops.repository.filesystem_scanner import scan_repository
from voxops.repository.path_policy import workspace_path
from voxops.storage.scan_store import ScanStore


# Lists the named steps used to scan and analyze a project.
# Also includes the error route used when a step cannot continue.
class Step(StrEnum):
    PREPARE = "prepare_scan"
    SCAN = "deterministic_scan"
    DETECT = "detect_units"
    RESOLVE = "optional_resolve_ambiguity"
    VALIDATE = "validate_findings"
    PERSIST = "persist_result"
    ERROR = "error"


# Holds the information passed between project analysis steps.
# Includes the input path, scan, detected apps, warnings, errors, and final result.
class StageState(TypedDict, total=False):
    scan_id: str
    input_path: str
    path: Path
    use_llm: bool
    scan: RepoScan
    candidate_units: list[DeployableUnit]
    final_units: list[DeployableUnit]
    warnings: list[str]
    error: ErrorDetail
    error_status: int
    ai_usage: AIUsage
    result: AnalysisResult
    status: Literal["scanning", "validated", "complete", "failed"]


# Runs the steps that scan a project and identify its app units.
# Optionally asks an AI to resolve uncertainty and saves the completed result.
class Stage1Workflow:
    # Connects the scanner, app detection, optional AI, and result storage.
    # Sets the order of steps and stops the workflow when a step reports an error.
    def __init__(
        self, settings: Settings, store: ScanStore, resolver: TopologyResolver | None = None
    ):
        self.settings, self.store = settings, store
        self.resolver = resolver or TopologyResolver(settings)
        builder = StateGraph(StageState)
        nodes = {
            Step.PREPARE: self.prepare_scan,
            Step.SCAN: self.deterministic_scan,
            Step.DETECT: self.detect_units,
            Step.RESOLVE: self.resolve_ambiguity,
            Step.VALIDATE: self.validate_findings,
            Step.PERSIST: self.persist_result,
        }
        for name, action in nodes.items():
            builder.add_node(name.value, action)
        builder.add_edge(START, Step.PREPARE.value)
        for source, target in [
            (Step.PREPARE, Step.SCAN),
            (Step.SCAN, Step.DETECT),
            (Step.RESOLVE, Step.VALIDATE),
            (Step.VALIDATE, Step.PERSIST),
        ]:
            builder.add_conditional_edges(
                source.value, self.continue_or_error, {True: target.value, False: END}
            )
        builder.add_conditional_edges(
            Step.DETECT.value,
            self.route_resolution,
            {Step.RESOLVE: Step.RESOLVE.value, Step.VALIDATE: Step.VALIDATE.value, Step.ERROR: END},
        )
        builder.add_edge(Step.PERSIST.value, END)
        self.graph = builder.compile()

    # Turns an application error into a failed workflow state.
    # Keeps its public message, category, and status for the caller.
    def failure(self, error: AppError) -> dict:
        return {
            "error": ErrorDetail(category=error.category, message=error.message),
            "error_status": error.status,
            "status": "failed",
        }

    # Checks whether the current analysis state contains an error.
    # Returns whether the workflow may continue to its next step.
    def continue_or_error(self, state: StageState) -> bool:
        return "error" not in state

    # Checks that the requested project path is inside an allowed folder.
    # Returns the prepared path and initial state, or records the path error.
    def prepare_scan(self, state: StageState) -> dict:
        try:
            return {
                "path": workspace_path(state["input_path"], self.settings.allowed_roots),
                "status": "scanning",
                "warnings": [],
            }
        except AppError as error:
            return self.failure(error)

    # Scans the project on a worker thread so file reads do not block other requests.
    # Returns the scan and warnings, or records an error if the project cannot be read.
    async def deterministic_scan(self, state: StageState) -> dict:
        try:
            scan = await asyncio.to_thread(
                scan_repository, state["path"], self.settings, state["scan_id"]
            )
            return {"scan": scan, "warnings": scan.warnings}
        except AppError as error:
            return self.failure(error)
        except OSError:
            return self.failure(AppError("scan_unavailable", "Repository could not be read.", 422))

    # Finds supported app units from the project scan.
    # Adds warnings when none are found or an AI model is unavailable.
    def detect_units(self, state: StageState) -> dict:
        units = detect_units(state["scan"])
        warnings = list(state["warnings"])
        if not units:
            warnings.append(
                "No supported deployable unit found; review manifests and scanner limits."
            )
        if not self.resolver.available() and (
            state["use_llm"] or any(unit.detection.unresolved_questions for unit in units)
        ):
            warnings.append("No configured model credential; deterministic analysis used.")
        return {
            "candidate_units": units,
            "final_units": units,
            "warnings": warnings,
            "ai_usage": AIUsage(
                status="unavailable" if not self.resolver.available() else "skipped"
            ),
        }

    # Chooses whether to ask the AI about uncertain app findings.
    # Continues directly to validation when no model is available or needed.
    def route_resolution(self, state: StageState) -> Step:
        if "error" in state:
            return Step.ERROR
        ambiguous = any(unit.detection.unresolved_questions for unit in state["candidate_units"])
        return (
            Step.RESOLVE
            if self.resolver.available() and (state["use_llm"] or ambiguous)
            else Step.VALIDATE
        )

    # Asks the configured resolver to assess the detected app units.
    # Returns its updated units, usage information, and any additional warnings.
    async def resolve_ambiguity(self, state: StageState) -> dict:
        units, usage, warnings = await self.resolver.resolve(state["candidate_units"])
        return {"final_units": units, "ai_usage": usage, "warnings": state["warnings"] + warnings}

    # Combines the scan and detected app units into an analysis result.
    # Includes unresolved questions, warnings, and whether an AI was used.
    def validate_findings(self, state: StageState) -> dict:
        units = state["final_units"]
        result = AnalysisResult(
            scan=state["scan"],
            detected_units=units,
            warnings=state["warnings"],
            unresolved_ambiguities=[
                f"{unit.root_path}: {question}"
                for unit in units
                for question in unit.detection.unresolved_questions
            ],
            ai_usage=state["ai_usage"],
            deterministic_only=state["ai_usage"].status != "used",
        )
        return {"result": result, "status": "validated"}

    # Saves the completed analysis result on a worker thread.
    # Marks the workflow complete or records a storage error.
    async def persist_result(self, state: StageState) -> dict:
        try:
            await asyncio.to_thread(self.store.save, state["result"])
            event("analysis_completed", state["scan_id"])
            return {"status": "complete"}
        except AppError as error:
            return self.failure(error)

    # Runs project analysis for the given path and optional AI request.
    # Returns the result or raises the error reported by a workflow step.
    async def analyze(self, path: str, use_llm: bool = False) -> AnalysisResult:
        with tracing_context(enabled=False):
            state = await self.graph.ainvoke(
                {"scan_id": uuid4().hex, "input_path": path, "use_llm": use_llm}
            )
        if "error" in state:
            error = state["error"]
            raise AppError(error.category, error.message, state["error_status"])
        return state["result"]
