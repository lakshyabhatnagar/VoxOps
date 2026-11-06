from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model as ProviderModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from voxops.core.settings import Settings


# Read the provider name and model name from the application's settings.
# Build its client using that provider's own key; NVIDIA calls go to NVIDIA's fixed address.
# Return nothing when the provider is unsupported or its key has not been supplied.
def create_model(settings: Settings) -> ProviderModel | None:
    provider, _, name = settings.model.partition(":")
    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicModel(
            name,
            provider=AnthropicProvider(
                anthropic_client=AsyncAnthropic(
                    api_key=settings.anthropic_api_key.get_secret_value(), max_retries=0
                )
            ),
        )
    if provider == "openai" and settings.openai_api_key:
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                openai_client=AsyncOpenAI(
                    api_key=settings.openai_api_key.get_secret_value(), max_retries=0
                )
            ),
        )
    if provider == "nvidia" and settings.nvidia_api_key:
        return OpenAIChatModel(
            name,
            provider=OpenAIProvider(
                openai_client=AsyncOpenAI(
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key=settings.nvidia_api_key.get_secret_value(),
                    max_retries=0,
                )
            ),
            settings=(
                {"openai_reasoning_effort": "low"}
                if name == "mistralai/mistral-large-4-1t-a52b"
                else None
            ),
        )
    return None
