from __future__ import annotations

from dataclasses import dataclass, field

from google.genai.types import Content, FunctionCall, GenerateContentResponse


@dataclass
class GeminiComputerUseState:
    """Conversation state for Gemini Computer Use sessions."""

    contents: list[Content]
    last_response: GenerateContentResponse | None = None
    last_function_calls: list[FunctionCall] = field(default_factory=list)
