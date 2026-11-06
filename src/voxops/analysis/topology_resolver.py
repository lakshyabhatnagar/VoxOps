import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from voxops.agents.topology_agent import TopologyOutput, create_agent
from voxops.core.settings import Settings
from voxops.domain.models import AIUsage, DeployableUnit, Evidence, Role


# Suggests how the discovered parts of a project fit together.
# Checks suggestions against known facts and falls back to the original results.
class TopologyResolver:
    # Sets up the optional assistant used to improve project analysis.
    # Keeps the settings and any assistant supplied by the caller.
    def __init__(self, settings: Settings, agent: Agent[None, TopologyOutput] | None = None):
        self.settings, self.agent = settings, agent

    # Check whether the caller supplied an assistant or configured a supported provider's key.
    # Match the key to the selected provider, so another service's key does not enable it.
    # Return false when analysis should continue using only the local detection rules.
    def available(self) -> bool:
        return self.agent is not None or bool(
            self.settings.model.startswith("anthropic:")
            and self.settings.anthropic_api_key
            or self.settings.model.startswith("openai:")
            and self.settings.openai_api_key
            or self.settings.model.startswith("nvidia:")
            and self.settings.nvidia_api_key
        )

    # Prepares project facts for the optional assistant.
    # Includes limited relative paths and facts while leaving out file contents and values.
    def context(self, units: list[DeployableUnit]) -> str:
        payload: list[dict] = []
        for unit in units:
            item = {
                "id": unit.id,
                "root": unit.root_path,
                "framework": unit.framework.value,
                "role": unit.role.value,
                "questions": unit.detection.unresolved_questions,
                "evidence": [
                    {"path": fact.path, "kind": fact.kind} for fact in unit.detection.evidence[:20]
                ],
            }
            candidate = json.dumps({"units": [*payload, item]}, ensure_ascii=True)
            if len(candidate) > self.settings.max_context_chars:
                break
            payload.append(item)
        return json.dumps({"units": payload}, ensure_ascii=True)

    # Applies useful suggestions to the discovered project parts.
    # Checks each suggestion and keeps the original results if the assistant fails.
    async def resolve(
        self, units: list[DeployableUnit]
    ) -> tuple[list[DeployableUnit], AIUsage, list[str]]:
        usage = AIUsage(status="failed", model=self.settings.model)
        try:
            agent = self.agent or create_agent(self.settings)
            if agent is None:
                return (
                    units,
                    AIUsage(status="unavailable"),
                    ["Model unavailable; deterministic analysis used."],
                )
            usage.requests = 1
            async with asyncio.timeout(self.settings.model_timeout):
                response = await agent.run(
                    self.context(units),
                    usage_limits=UsageLimits(request_limit=1, output_tokens_limit=1500),
                    model_settings={"max_tokens": 1200, "timeout": self.settings.model_timeout},
                )
            output = TopologyOutput.model_validate(response.output)
            measured = response.usage
            usage = AIUsage(
                status="used",
                model=self.settings.model,
                requests=measured.requests,
                input_tokens=measured.input_tokens,
                output_tokens=measured.output_tokens,
            )
            resolved = [unit.model_copy(deep=True) for unit in units]
            warnings = []
            known = {unit.id: unit for unit in resolved}
            for assessment in output.assessments:
                unit = known.get(assessment.unit_id)
                if unit is None:
                    warnings.append("Model referenced an unknown unit; suggestion ignored.")
                    continue
                if unit.role == Role.UNKNOWN and assessment.conclusion == "supported":
                    if assessment.role not in {Role.SSR, Role.FULLSTACK}:
                        continue
                    unit.role = assessment.role
                    unit.detection.evidence.append(
                        Evidence(
                            path=unit.root_path,
                            kind="topology_inference",
                            field=assessment.role.value,
                            confidence=assessment.confidence,
                            source="llm",
                        )
                    )
                elif assessment.role != unit.role:
                    warnings.append(
                        "Model suggestion contradicted deterministic classification; ignored."
                    )
            return resolved, usage, sorted(set(warnings))
        except Exception:
            return (
                units,
                usage,
                ["Optional model failed or timed out; deterministic findings retained."],
            )
