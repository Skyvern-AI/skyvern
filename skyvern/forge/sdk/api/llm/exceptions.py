from skyvern.exceptions import SkyvernException
from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

# Exception type name constants
LLM_PROVIDER_ERROR_TYPE = "LLMProviderError"
LLM_PROVIDER_ERROR_RETRYABLE_TASK_TYPE = "LLMProviderErrorRetryableTask"


class BaseLLMError(SkyvernException):
    # Stamped by the handler the error escaped from. Response-shape errors are raised deep in
    # parsing, where neither is known, so callers that only see the exception (e.g. the complete
    # verification) can't otherwise say which model route and prompt produced it.
    llm_key: str | None = None
    prompt_name: str | None = None


class MissingLLMProviderEnvVarsError(BaseLLMError):
    def __init__(self, llm_key: str, missing_env_vars: list[str]) -> None:
        super().__init__(f"Environment variables {','.join(missing_env_vars)} are required for LLMProvider {llm_key}")


class EmptyLLMResponseError(BaseLLMError):
    def __init__(self, response: str) -> None:
        super().__init__(f"LLM response content is empty: {response}")


class InvalidLLMResponseFormat(BaseLLMError):
    def __init__(self, response: str) -> None:
        super().__init__(f"LLM response content is not a valid JSON: {response}")


class InvalidLLMResponseType(BaseLLMError):
    def __init__(self, response_type: str) -> None:
        super().__init__(f"LLM response content is expected to be a dict, but got {response_type}")


class LLMResponseMissingActionsError(InvalidLLMResponseFormat):
    def __init__(self, response_keys: list[str]) -> None:
        # Skip InvalidLLMResponseFormat.__init__ — the response parsed fine, it just lacks the actions field
        SkyvernException.__init__(
            self,
            f"LLM response does not contain a usable 'actions' list; received keys: {response_keys}",
        )


class DuplicateCustomLLMProviderError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"Custom LLMProvider {llm_key} is already registered")


class DuplicateLLMConfigError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"LLM config with key {llm_key} is already registered")


class InvalidLLMConfigError(BaseLLMError):
    def __init__(self, llm_key: str) -> None:
        super().__init__(f"LLM config with key {llm_key} is not a valid LLMConfig")


def _cause_detail(cause: BaseException | None) -> str:
    return f" — {type(cause).__name__}: {cause}" if cause else ""


class WithheldProviderError(Exception):
    """Stands in for a provider error whose message may quote the document content it was sent."""

    def __init__(self, error: BaseException) -> None:
        status_code = getattr(error, "status_code", None)
        self.status_code = status_code if isinstance(status_code, int) else None
        self.error_type = f"{type(error).__module__}.{type(error).__name__}"
        self.message_fingerprint = diagnostic_fingerprint(str(error))
        details = [f"status {self.status_code}"] if self.status_code is not None else []
        if self.status_code == 429 or "RateLimit" in type(error).__name__:
            # The run failure classifier attributes a rate limit to the LLM only when these words appear.
            details.append("an LLM rate limit")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(
            f"{type(error).__name__}{suffix}; provider message withheld, fingerprint {self.message_fingerprint}"
        )


def cause_type_name(error: BaseException) -> str | None:
    cause = error.__cause__
    if isinstance(cause, WithheldProviderError):
        return cause.error_type.rpartition(".")[2]
    return type(cause).__name__ if cause is not None else None


class LLMProviderError(BaseLLMError):
    def __init__(self, llm_key: str, cause: Exception | None = None) -> None:
        super().__init__(f"Error while using LLMProvider {llm_key}{_cause_detail(cause)}")


class LLMProviderErrorRetryableTask(LLMProviderError):
    def __init__(self, llm_key: str, cause: Exception | None = None) -> None:
        # Skip LLMProviderError.__init__ to set our own message prefix
        SkyvernException.__init__(self, f"Retryable error while using LLMProvider {llm_key}{_cause_detail(cause)}")


def withhold_provider_text(error: LLMProviderError) -> None:
    cause = error.__cause__
    if cause is None or isinstance(cause, WithheldProviderError):
        return
    stand_in = WithheldProviderError(cause)
    message = str(error)
    if message.endswith(old_detail := _cause_detail(cause)):
        message = message[: -len(old_detail)] + _cause_detail(stand_in)
    error.message = message
    error.args = (message,)
    error.__cause__ = stand_in
    # The original error stays reachable through __context__ unless it is dropped too.
    error.__context__ = None
    error.__suppress_context__ = True


class LLMOutputTruncatedError(InvalidLLMResponseFormat):
    def __init__(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
        finish_reason: str | None = None,
    ) -> None:
        # The reason is reported, not assumed: upstreams label an answer-less reply `length` or
        # `stop` for the same underlying spend, and the two are only tellable apart here.
        super().__init__(
            f"LLM output truncated (finish_reason={finish_reason}, content=None). "
            f"model={model}, prompt_tokens={prompt_tokens}, completion_tokens={completion_tokens}, "
            f"reasoning_tokens={reasoning_tokens}"
        )
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.reasoning_tokens = reasoning_tokens
        self.finish_reason = finish_reason


# A response-shape error rather than a provider error: the model answered, so callers that hold or re-ask on an
# unusable reply keep doing that instead of treating the model as unreachable.
class LLMContentFilteredError(InvalidLLMResponseFormat):
    def __init__(self, llm_key: str, models: list[str]) -> None:
        SkyvernException.__init__(
            self,
            f"The model refused or its content filter blocked the response on {', then '.join(models)} "
            f"(LLM key {llm_key})",
        )
        self.llm_key = llm_key


class NoProviderEnabledError(BaseLLMError):
    def __init__(self) -> None:
        super().__init__(
            "At least one LLM provider must be enabled. Run `skyvern init` and follow through the LLM provider setup, "
            "or update the .env file (check out .env.example to see the required environment variables)."
        )
