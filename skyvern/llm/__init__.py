from skyvern.llm.providers.ollama import OllamaProvider

PROVIDERS = {
    # ... other providers ...
    "OLLAMA": OllamaProvider,
}

def get_provider():
    import os
    key = os.getenv("LLM_KEY", "").upper()

    if key == "OLLAMA":
        return OllamaProvider(
            server_url=os.getenv("OLLAMA_SERVER_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_MODEL", "deepseek-coder:6.7b")
        )
    elif key in PROVIDERS:
        return PROVIDERS[key]()
    else:
        raise ValueError(f"Unknown LLM provider: {key}")
