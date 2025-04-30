import os
import requests
from skyvern.llm.base import BaseLLMProvider
from skyvern.config import Settings

class OpenRouterProvider(BaseLLMProvider):
    """
    Provider for OpenRouter's OpenAI-compatible API.
    """

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.api_base = "https://openrouter.ai/api/v1"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def call(self, prompt: str, **kwargs) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", Settings.LLM_CONFIG_MAX_TOKENS),
        }

        response = requests.post(
            f"{self.api_base}/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()

        return response.json()["choices"][0]["message"]["content"]
