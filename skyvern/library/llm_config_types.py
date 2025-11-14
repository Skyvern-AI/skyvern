from dataclasses import dataclass


@dataclass
class AzureConfig:
    """Configuration for Azure OpenAI."""

    api_key: str | None = None
    deployment: str | None = None
    api_base: str | None = None
    api_version: str | None = None


@dataclass
class VertexConfig:
    """Configuration for Google Vertex AI."""

    credentials: str | None = None
    project_id: str | None = None
    location: str | None = None


@dataclass
class GroqConfig:
    """Configuration for Groq."""

    api_key: str | None = None
    model: str | None = None
    api_base: str | None = None
