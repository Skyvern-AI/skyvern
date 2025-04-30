from skyvern.llm.providers.ollama import OllamaProvider

from skyvern.config import Settings

PROVIDERS = {
    # ... other providers ...
    "OLLAMA": OllamaProvider,
}

def get_provider():
    key = Settings.LLM_KEY
    
    if key == "OLLAMA":
        return OllamaProvider(
            server_url=Settings.OLLAMA_SERVER_URL,
            model=Settings.OLLAMA_MODEL,
        )
    elif key in PROVIDERS:
        return PROVIDERS[key]()
    else:
        raise ValueError(f"Unknown LLM provider: {key}")
