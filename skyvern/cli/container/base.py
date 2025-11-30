from abc import ABC, abstractmethod

from .models import ContainerRuntime, ExecResult


class BaseContainerRuntime(ABC):
    """Abstract base class for container runtime operations."""

    @property
    @abstractmethod
    def runtime_type(self) -> ContainerRuntime:
        """Return the runtime type identifier."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return a human-readable name for the runtime."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the runtime binary is available on the system."""

    @abstractmethod
    def is_running(self) -> bool:
        """Check if the runtime daemon/service is running and accessible."""

    @abstractmethod
    def run_container(
        self,
        image: str,
        name: str,
        *,
        ports: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
        detach: bool = True,
    ) -> tuple[str | None, int]:
        """Run a new container."""

    @abstractmethod
    def start_container(self, container_name: str) -> tuple[str | None, int]:
        """Start an existing stopped container."""

    @abstractmethod
    def stop_container(self, container_name: str, timeout: int = 10) -> tuple[str | None, int]:
        """Stop a running container."""

    @abstractmethod
    def remove_container(self, container_name: str, force: bool = False) -> tuple[str | None, int]:
        """Remove a container."""

    @abstractmethod
    def container_exists(self, container_name: str) -> bool:
        """Check if a container exists (running or stopped)."""

    @abstractmethod
    def is_container_running(self, container_name: str) -> bool:
        """Check if a specific container is currently running."""

    @abstractmethod
    def exec_in_container(
        self,
        container_name: str,
        command: list[str],
        *,
        user: str | None = None,
    ) -> ExecResult:
        """Execute a command inside a running container."""

    @abstractmethod
    def get_compose_command(self) -> list[str]:
        """Get the compose command for this runtime."""
