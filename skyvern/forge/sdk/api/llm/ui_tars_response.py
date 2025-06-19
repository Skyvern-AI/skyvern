"""UI-TARS response model that mimics the ModelResponse interface."""

import json
from typing import Any

from anthropic import BaseModel


class Message:
    def __init__(self, content: str):
        self.content = content
        self.role = "assistant"


class Choice:
    def __init__(self, content: str):
        self.message = Message(content)


class UITarsResponse(BaseModel):
    """A response object that mimics the ModelResponse interface for UI-TARS API responses."""

    def __init__(self, content: str, model: str):
        # Create choice objects with proper nested structure for parse_api_response
        self.choices = [Choice(content)]
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.model = model
        self.object = "chat.completion"

    def model_dump_json(self, indent: int = 2) -> str:
        """Provide model_dump_json compatibility for artifact creation."""
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": self.choices[0].message.content,
                            "role": self.choices[0].message.role,
                        }
                    }
                ],
                "usage": self.usage,
                "model": self.model,
                "object": self.object,
            },
            indent=indent,
        )

    def model_dump(self, exclude_none: bool = True) -> dict:
        """Provide model_dump compatibility for raw_response."""
        return {
            "choices": [
                {"message": {"content": self.choices[0].message.content, "role": self.choices[0].message.role}}
            ],
            "usage": self.usage,
            "model": self.model,
            "object": self.object,
        }

    def get(self, key: str, default: Any = None) -> Any:
        """Provide dict-like access for compatibility."""
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        """Provide dict-like access for compatibility."""
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        """Provide dict-like access for compatibility."""
        return hasattr(self, key)
