"""Data models for container runtime abstraction."""

from dataclasses import dataclass
from enum import StrEnum


class ContainerRuntime(StrEnum):
    """Supported container runtimes."""

    DOCKER = "docker"
    PODMAN = "podman"


class ContainerState(StrEnum):
    """Container lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    EXITED = "exited"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass
class ExecResult:
    """Result of executing a command in a container."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        """Return True if the command executed successfully."""
        return self.exit_code == 0
