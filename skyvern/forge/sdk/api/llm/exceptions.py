from skyvern.exceptions import SkyvernException


class BaseLLMError(SkyvernException):
    pass


class MissingLLMProviderEnvVarsError(BaseLLMError):
    def __init__(self, llm_key: str, missing_env_vars: list[str]) -> None:
        super().__init__(f"Environment variables {','.join(missing_env_vars)} are required for LLMProvider {llm_key}")


class EmptyLLMResponseError(BaseLLMError):
    def __init__(self, response: str) -> None:
        super().__init__(f"LLM response content is empty: {response}")


class InvalidLLMResponseFormat(BaseLLMError):
    def __init__(self, response: str) -> None:
        super().__init__(f"LLM response content is not a valid JSON: {response}")


class DuplicateCustomLLMProviderError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"Custom LLMProvider {llm_key} is already registered")


class DuplicateLLMConfigError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"LLM config with key {llm_key} is already registered")


class InvalidLLMConfigError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"LLM config with key {llm_key} is not a valid LLMConfig")


class LLMProviderError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"Error while using LLMProvider {llm_key}")


class LLMProviderErrorRetryableTask(LLMProviderError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"Retryable error while using LLMProvider {llm_key}")


class NoProviderEnabledError(BaseLLMError):
    def __init__(self) -> None:
        super().__init__(
            "At least one LLM provider must be enabled. Run setup.sh and follow through the LLM provider setup, or "
            "update the .env file (check out .env.example to see the required environment variables)."
        )
