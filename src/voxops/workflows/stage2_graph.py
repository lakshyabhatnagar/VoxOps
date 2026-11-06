import asyncio
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langsmith import tracing_context

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import ArtifactPlan, TaskResult


# Combines two sets of results indexed by file job.
# Raises an error if the same job has two different results.
def merge_results(
    left: dict[str, TaskResult], right: dict[str, TaskResult]
) -> dict[str, TaskResult]:
    result = dict(left)
    for key, value in right.items():
        if key in result and result[key] != value:
            raise AppError("task_result_conflict", "A task returned conflicting proposals.", 409)
        result[key] = value
    return result


# Holds the run, project folder, plan, and file job results.
# Combines incoming results while checking for conflicting updates.
class ArtifactState(TypedDict):
    run_id: str
    root: Path
    plan: ArtifactPlan
    results: Annotated[dict[str, TaskResult], merge_results]


# Runs file jobs once their required earlier jobs have succeeded.
# Allows several jobs at once while sharing one AI request budget.
class Stage2Graph:
    # Sets up the file generator, shared AI budget, and concurrency limit.
    # Connects the steps that dispatch jobs and collect their results.
    def __init__(self, settings: Settings, generator: ArtifactGenerator, budget: ModelBudget):
        self.generator, self.budget = generator, budget
        self.semaphore = asyncio.Semaphore(settings.artifact_concurrency)
        builder = StateGraph(ArtifactState)
        builder.add_node("dispatch", self.barrier)
        builder.add_node("worker", self.worker)
        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges("dispatch", self.ready)
        builder.add_edge("worker", "dispatch")
        self.graph = builder.compile()

    # Waits for the current group of jobs to finish before choosing more work.
    # Returns no state changes when the workflow reaches this step.
    def barrier(self, state: ArtifactState) -> dict:
        return {}

    # Finds unfinished jobs whose required jobs have passed or been skipped.
    # Sends those jobs to workers, or ends when none are ready.
    def ready(self, state: ArtifactState) -> list[Send] | str:
        results = state["results"]
        ready = [
            task
            for key, task in state["plan"].tasks.items()
            if key not in results
            and all(
                dependency in results
                and results[dependency].status in {"passed", "validated_existing", "skipped"}
                for dependency in task.depends_on
            )
        ]
        return [
            Send(
                "worker",
                {
                    "run_id": state["run_id"],
                    "root": state["root"],
                    "plan": state["plan"],
                    "task": task,
                },
            )
            for task in ready
        ] or END

    # Runs one file job while respecting the concurrency limit.
    # Returns its result or marks the job blocked if an error occurs.
    async def worker(self, state: dict) -> dict:
        task = state["task"]
        async with self.semaphore:
            try:
                result = await self.generator.generate(
                    state["run_id"], state["root"], task, state["plan"], self.budget
                )
            except AppError as error:
                result = TaskResult(
                    task_id=task.task_id, status="blocked", error_category=error.category
                )
            except Exception:
                result = TaskResult(
                    task_id=task.task_id, status="blocked", error_category="artifact_worker_failed"
                )
        return {"results": {task.task_id: result}}

    # Runs the plan while keeping any previously completed job results.
    # Marks jobs that could not run because of failed dependencies as blocked.
    async def execute(
        self,
        run_id: str,
        root: Path,
        plan: ArtifactPlan,
        prior: dict[str, TaskResult] | None = None,
    ) -> dict[str, TaskResult]:
        complete = {
            key: value
            for key, value in (prior or {}).items()
            if value.status in {"passed", "validated_existing", "skipped"}
        }
        with tracing_context(enabled=False):
            state = await self.graph.ainvoke(
                {"run_id": run_id, "root": root, "plan": plan, "results": complete},
                {"recursion_limit": max(30, len(plan.tasks) * 3)},
            )
        results = state["results"]
        for key in plan.tasks.keys() - results.keys():
            results[key] = TaskResult(
                task_id=key, status="blocked", error_category="dependency_failed"
            )
        return results
