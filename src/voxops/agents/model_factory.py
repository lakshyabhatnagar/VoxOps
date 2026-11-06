from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model as ProviderModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from voxops.core.settings import Settings


# Builds the configured Anthropic or OpenAI model when its API key is available.
# Returns nothing for an unsupported provider or a missing key.
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
    return None
