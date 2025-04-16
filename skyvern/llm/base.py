# skyvern/llm/base.py

class BaseLLMProvider:
    def call(self, prompt: str, **kwargs) -> str:
        raise NotImplementedError("Subclasses must implement this method.")
