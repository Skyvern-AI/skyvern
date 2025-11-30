import shutil
import subprocess

from .base import BaseContainerRuntime
from .models import ContainerRuntime, ExecResult


class DockerRuntime(BaseContainerRuntime):
    """Docker container runtime implementation."""

    @property
    def runtime_type(self) -> ContainerRuntime:
        """Return the runtime type identifier."""
        return ContainerRuntime.DOCKER

    @property
    def display_name(self) -> str:
        """Return a human-readable name for the runtime."""
        return "Docker"

    def is_available(self) -> bool:
        """Check if Docker is available on the system."""
        return shutil.which("docker") is not None

    def is_running(self) -> bool:
        """Check if Docker daemon is running."""
        if not self.is_available():
            return False
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, subprocess.TimeoutExpired):
            return False

    def run_container(
        self,
        image: str,
        name: str,
        *,
        ports: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
        detach: bool = True,
    ) -> tuple[str | None, int]:
        """Run a new Docker container."""
        cmd = ["docker", "run", "--name", name]

        if detach:
            cmd.append("-d")

        if ports:
            for host_port, container_port in ports.items():
                cmd.extend(["-p", f"{host_port}:{container_port}"])

        if environment:
            for key, value in environment.items():
                cmd.extend(["-e", f"{key}={value}"])

        cmd.append(image)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip() if result.stdout else None, result.returncode
        except subprocess.SubprocessError:
            return None, 1

    def start_container(self, container_name: str) -> tuple[str | None, int]:
        """Start an existing Docker container."""
        try:
            result = subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() if result.stdout else None, result.returncode
        except subprocess.SubprocessError:
            return None, 1

    def stop_container(self, container_name: str, timeout: int = 10) -> tuple[str | None, int]:
        """Stop a running Docker container."""
        try:
            result = subprocess.run(
                ["docker", "stop", "-t", str(timeout), container_name],
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() if result.stdout else None, result.returncode
        except subprocess.SubprocessError:
            return None, 1

    def remove_container(self, container_name: str, force: bool = False) -> tuple[str | None, int]:
        """Remove a Docker container."""
        cmd = ["docker", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(container_name)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip() if result.stdout else None, result.returncode
        except subprocess.SubprocessError:
            return None, 1

    def container_exists(self, container_name: str) -> bool:
        """Check if a Docker container exists."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            return container_name in result.stdout.strip().split("\n")
        except subprocess.SubprocessError:
            return False

    def is_container_running(self, container_name: str) -> bool:
        """Check if a Docker container is running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            return container_name in result.stdout.strip().split("\n")
        except subprocess.SubprocessError:
            return False

    def exec_in_container(
        self,
        container_name: str,
        command: list[str],
        *,
        user: str | None = None,
    ) -> ExecResult:
        """Execute a command inside a Docker container."""
        cmd = ["docker", "exec"]

        if user:
            cmd.extend(["-u", user])

        cmd.append(container_name)
        cmd.extend(command)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return ExecResult(
                exit_code=result.returncode,
                stdout=result.stdout.strip(),
                stderr=result.stderr.strip(),
            )
        except subprocess.SubprocessError as e:
            return ExecResult(exit_code=1, stdout="", stderr=str(e))

    def get_compose_command(self) -> list[str]:
        """Get the Docker Compose command."""
        return ["docker", "compose"]
