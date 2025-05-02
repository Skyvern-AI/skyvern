from skyvern.llm.providers.ollama import OllamaProvider
from skyvern.llm.providers.openrouter import OpenRouterProvider

from skyvern.config import Settings

PROVIDERS = {
    # ... other providers ...
    "OLLAMA": OllamaProvider,
    "OPENROUTER": OpenRouterProvider,
}

def get_provider():
    key = Settings.LLM_KEY

    if key == "OLLAMA":
        return OllamaProvider(
            server_url=Settings.OLLAMA_SERVER_URL,
            model=Settings.OLLAMA_MODEL,
        )
    elif key == "OPENROUTER":
        return OpenRouterProvider(
            api_key=Settings.OPENROUTER_API_KEY,
            model=Settings.OPENROUTER_MODEL,
        )
    elif key in PROVIDERS:
        return PROVIDERS[key]()
    else:
        raise ValueError(f"Unknown LLM provider: {key}")