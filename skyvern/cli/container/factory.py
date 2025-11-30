import os

from .base import BaseContainerRuntime
from .docker import DockerRuntime
from .models import ContainerRuntime
from .podman import PodmanRuntime


class ContainerRuntimeFactory:
    """Factory for creating and managing container runtime instances.

    The factory supports:
    1. Explicit runtime selection via set_runtime()
    2. Auto-detection based on available binaries
    3. Configuration via environment variable SKYVERN_CONTAINER_RUNTIME
    """

    _runtime: BaseContainerRuntime | None = None

    @classmethod
    def set_runtime(cls, runtime: BaseContainerRuntime) -> None:
        """Explicitly set the container runtime to use.

        Args:
            runtime: The container runtime instance to use
        """
        cls._runtime = runtime

    @classmethod
    def get_runtime(cls) -> BaseContainerRuntime:
        """Get the current container runtime instance.

        If no runtime has been set, attempts auto-detection in order:
        1. Check SKYVERN_CONTAINER_RUNTIME environment variable
        2. Try Docker
        3. Try Podman
        4. Raise RuntimeError if nothing available

        Returns:
            The container runtime instance

        Raises:
            RuntimeError: If no container runtime is available
        """
        if cls._runtime is not None:
            return cls._runtime

        cls._runtime = cls._auto_detect_runtime()
        return cls._runtime

    @classmethod
    def reset(cls) -> None:
        """Reset the factory state.

        This clears the cached runtime instance, useful for testing.
        """
        cls._runtime = None

    @classmethod
    def _create_runtime(cls, runtime_type: ContainerRuntime) -> BaseContainerRuntime:
        """Create a runtime instance for the given type.

        Args:
            runtime_type: The type of runtime to create

        Returns:
            A new runtime instance

        Raises:
            ValueError: If the runtime type is not supported
        """
        if runtime_type == ContainerRuntime.DOCKER:
            return DockerRuntime()
        elif runtime_type == ContainerRuntime.PODMAN:
            return PodmanRuntime()
        else:
            raise ValueError(f"Unsupported container runtime: {runtime_type}")

    @classmethod
    def _auto_detect_runtime(cls) -> BaseContainerRuntime:
        """Auto-detect available container runtime.

        Returns:
            A runtime instance for the first available runtime

        Raises:
            RuntimeError: If no runtime is available
        """
        # Check environment variable first
        env_runtime = os.environ.get("SKYVERN_CONTAINER_RUNTIME", "").lower()
        if env_runtime:
            try:
                runtime_type = ContainerRuntime(env_runtime)
                runtime = cls._create_runtime(runtime_type)
                if runtime.is_available() and runtime.is_running():
                    return runtime
            except ValueError:
                pass

        # Try Docker first (more common)
        docker = DockerRuntime()
        if docker.is_available() and docker.is_running():
            return docker

        # Try Podman
        podman = PodmanRuntime()
        if podman.is_available() and podman.is_running():
            return podman

        raise RuntimeError(
            "No container runtime available. Please install and start Docker or Podman.\n"
            "Docker: https://www.docker.com/get-started\n"
            "Podman: https://podman.io/get-started"
        )
