#!/usr/bin/env python3
"""
Script to generate locked requirements.txt using uv from pyproject.toml

This script uses uv to generate a locked requirements.txt file from the Poetry
pyproject.toml configuration, ensuring reproducible builds in Docker environments.
"""

import subprocess
import sys
from pathlib import Path


def run_command(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a command and return exit code, stdout, and stderr."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as e:
        return 1, "", f"Command not found: {e}"


def check_uv_installed() -> bool:
    """Check if uv is installed and available."""
    code, _, _ = run_command(["uv", "--version"])
    return code == 0


def install_uv() -> bool:
    """Install uv if not already available."""
    print("Installing uv...")
    code, stdout, stderr = run_command([sys.executable, "-m", "pip", "install", "uv"])
    if code != 0:
        print(f"Failed to install uv: {stderr}")
        return False
    print("uv installed successfully")
    return True


def generate_requirements(project_dir: Path) -> bool:
    """Generate requirements.txt using uv from pyproject.toml."""
    print(f"Generating requirements.txt from {project_dir}/pyproject.toml...")
    
    # Use uv pip compile to generate requirements.txt from pyproject.toml
    cmd = [
        "uv", "pip", "compile", 
        str(project_dir / "pyproject.toml"),
        "--output-file", str(project_dir / "requirements.txt"),
        "--generate-hashes",
        "--no-deps"  # Don't include dev dependencies
    ]
    
    code, stdout, stderr = run_command(cmd, cwd=project_dir)
    
    if code != 0:
        print(f"Failed to generate requirements.txt: {stderr}")
        return False
    
    print("requirements.txt generated successfully")
    if stdout:
        print(f"uv output: {stdout}")
    
    return True


def main():
    """Main function to generate requirements.txt."""
    project_dir = Path(__file__).parent.resolve()
    
    print(f"Working in directory: {project_dir}")
    
    # Check if pyproject.toml exists
    pyproject_path = project_dir / "pyproject.toml"
    if not pyproject_path.exists():
        print(f"Error: pyproject.toml not found in {project_dir}")
        sys.exit(1)
    
    # Check if uv is installed
    if not check_uv_installed():
        if not install_uv():
            sys.exit(1)
    
    # Generate requirements.txt
    if not generate_requirements(project_dir):
        sys.exit(1)
    
    # Verify the generated file
    requirements_path = project_dir / "requirements.txt"
    if requirements_path.exists():
        print(f"✅ Successfully generated {requirements_path}")
        print(f"File size: {requirements_path.stat().st_size} bytes")
    else:
        print("❌ Failed to generate requirements.txt")
        sys.exit(1)


if __name__ == "__main__":
    main()