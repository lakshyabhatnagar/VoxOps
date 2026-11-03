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


# Why: Keep graph destinations under application control rather than model-generated strings.
# How: Centralize node names and conditional transition labels in one enum.
class Step(StrEnum):
    PREPARE = "prepare_scan"
    SCAN = "deterministic_scan"
    DETECT = "detect_units"
    RESOLVE = "optional_resolve_ambiguity"
    VALIDATE = "validate_findings"
    PERSIST = "persist_result"
    ERROR = "error"


# Why: Make each stage's local-only input, findings and failures explicit.
# How: Use a typed state with structured optional result/error and stable status values.
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


# Why: Execute precisely the Stage 1 pipeline with safe skips and typed failure propagation.
# How: Bind scanner, adapters, specialist and store into a small compiled LangGraph.
class Stage1Workflow:
    # Why: Share dependencies without adding an unrestricted supervisor or project execution tools.
    # How: Register a fixed graph whose conditional routes can only proceed or terminate.
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

    # Why: Preserve an original safe error category across graph transitions.
    # How: Store only public error fields and a terminal status in state.
    def failure(self, error: AppError) -> dict:
        return {
            "error": ErrorDetail(category=error.category, message=error.message),
            "error_status": error.status,
            "status": "failed",
        }

    # Why: Stop failed stages instead of running downstream nodes on absent findings.
    # How: Route exclusively on the presence of a structured error.
    def continue_or_error(self, state: StageState) -> bool:
        return "error" not in state

    # Why: Canonicalize untrusted input before any scanner or Git operation.
    # How: Apply configured workspace containment and preserve policy failures as graph data.
    def prepare_scan(self, state: StageState) -> dict:
        try:
            return {
                "path": workspace_path(state["input_path"], self.settings.allowed_roots),
                "status": "scanning",
                "warnings": [],
            }
        except AppError as error:
            return self.failure(error)

    # Why: Keep blocking local reads off the API event loop.
    # How: Run the bounded deterministic scanner in a worker thread and retain safe errors.
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

    # Why: Discover zero, one or many units without forcing an application topology.
    # How: Apply the framework registry to sanitized manifests and source facts.
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

    # Why: Call the specialist only for ambiguity or an explicit supplementary-analysis request.
    # How: Combine local availability and typed deterministic uncertainty into a fixed route enum.
    def route_resolution(self, state: StageState) -> Step:
        if "error" in state:
            return Step.ERROR
        ambiguous = any(unit.detection.unresolved_questions for unit in state["candidate_units"])
        return (
            Step.RESOLVE
            if self.resolver.available() and (state["use_llm"] or ambiguous)
            else Step.VALIDATE
        )

    # Why: Supplement deterministic facts without letting model errors fail a valid scan.
    # How: Use the bounded resolver and retain its usage and fallback warnings.
    async def resolve_ambiguity(self, state: StageState) -> dict:
        units, usage, warnings = await self.resolver.resolve(state["candidate_units"])
        return {"final_units": units, "ai_usage": usage, "warnings": state["warnings"] + warnings}

    # Why: Guarantee serialization-valid stable findings before persistence or API return.
    # How: Build the aggregate model, retaining explicit unresolved adapter questions.
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

    # Why: Finish only after a complete local result is stored successfully.
    # How: Atomically persist the validated result and log only its generated identifier.
    async def persist_result(self, state: StageState) -> dict:
        try:
            await asyncio.to_thread(self.store.save, state["result"])
            event("analysis_completed", state["scan_id"])
            return {"status": "complete"}
        except AppError as error:
            return self.failure(error)

    # Why: Expose a typed application boundary while preserving graph errors for HTTP handling.
    # How: Generate a run ID, invoke the compiled graph and unwrap its validated aggregate.
    async def analyze(self, path: str, use_llm: bool = False) -> AnalysisResult:
        with tracing_context(enabled=False):
            state = await self.graph.ainvoke(
                {"scan_id": uuid4().hex, "input_path": path, "use_llm": use_llm}
            )
        if "error" in state:
            error = state["error"]
            raise AppError(error.category, error.message, state["error_status"])
        return state["result"]
