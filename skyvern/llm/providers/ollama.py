import os
import requests
from skyvern.llm.base import BaseLLMProvider

class OllamaProvider(BaseLLMProvider):
    """
    Provider for Ollama's OpenAI-compatible local server.
    """

    def __init__(self, server_url: str = "http://localhost:11434", model: str = "deepseek-coder:6.7b"):
        self.server_url = server_url.rstrip("/")
        self.model = model
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer not_needed"  # Not used by Ollama but required by interface
        }

    def call(self, prompt: str, **kwargs) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 1024),
        }

        response = requests.post(
            f"{self.server_url}/v1/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()

        return response.json()["choices"][0]["message"]["content"]
