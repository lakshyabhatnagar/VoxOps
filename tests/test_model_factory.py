import asyncio
import json
from functools import partial

import httpx
import pytest
from openai import AsyncOpenAI

from voxops.agents.artifact_agent import create_artifact_agent
from voxops.agents.model_factory import create_model
from voxops.agents.topology_agent import create_agent
from voxops.analysis.topology_resolver import TopologyResolver
from voxops.core.settings import Settings

NVIDIA_MODEL = "mistralai/mistral-large-4-1t-a52b"


# Select NVIDIA while only an OpenAI key is available.
# Check that VoxOps does not send the other provider's key to NVIDIA.
# A missing NVIDIA key must leave model access unavailable rather than choose another service.
def test_nvidia_requires_its_own_key():
    settings = Settings(model=f"nvidia:{NVIDIA_MODEL}", OPENAI_API_KEY="other-provider-key")
    assert create_model(settings) is None
    assert not TopologyResolver(settings).available()


# Configure NVIDIA and request optional AI help during repository analysis.
# Check that Stage 1 recognizes the NVIDIA key without requiring an OpenAI or Anthropic key.
# This checks availability only and does not contact the model service.
def test_nvidia_is_available_to_stage1():
    settings = Settings(model=f"nvidia:{NVIDIA_MODEL}", NVIDIA_API_KEY="nvapi-test-only")
    assert TopologyResolver(settings).available()


@pytest.mark.parametrize("kind", ["artifact", "topology"])
# Send real SDK requests through an in-memory HTTP server response for each AI use.
# Check the NVIDIA address, key, chosen model, and structured response request.
# Return a tool response and require the real PydanticAI agent to turn it into its output object.
def test_nvidia_structured_responses_use_the_configured_endpoint(monkeypatch, kind):
    requests = []
    expected = (
        {"target_path": "Dockerfile", "content": "FROM python:3.12-slim\n"}
        if kind == "artifact"
        else {"assessments": []}
    )

    # Handle one SDK request without opening a network connection.
    # Reject an incorrect address or key and require the model to return structured data.
    # Reply using the function name requested by PydanticAI, just as the NVIDIA endpoint would.
    def respond(request):
        assert str(request.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer nvapi-test-only"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == NVIDIA_MODEL
        assert body["tool_choice"] == "required"
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 512
        function = body["tools"][0]["function"]["name"]
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": NVIDIA_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "abc123xyz",
                                    "type": "function",
                                    "function": {
                                        "name": function,
                                        "arguments": json.dumps(expected),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    # Give the real OpenAI SDK a mock HTTP connection while keeping factory construction unchanged.
    # Run either Stage 1's analysis agent or Stage 2's file agent using only the NVIDIA key.
    # Close the connection after reading the parsed result and recorded token usage.
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(
                "voxops.agents.model_factory.AsyncOpenAI",
                partial(AsyncOpenAI, http_client=client),
            )
            settings = Settings(model=f"nvidia:{NVIDIA_MODEL}", NVIDIA_API_KEY="nvapi-test-only")
            agent = (
                create_artifact_agent(settings) if kind == "artifact" else create_agent(settings)
            )
            assert agent is not None
            return await agent.run(
                "Return the requested example.", model_settings={"max_tokens": 512}
            )

    result = asyncio.run(run())
    for key, value in expected.items():
        assert getattr(result.output, key) == value
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 20
    assert len(requests) == 1
