import platform
import subprocess
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from skyvern.constants import REPO_ROOT_DIR


def migrate_db() -> None:
    alembic_cfg = Config()
    path = f"{REPO_ROOT_DIR}/alembic"
    alembic_cfg.set_main_option("script_location", path)
    command.upgrade(alembic_cfg, "head")


def detect_os() -> str:
    """
    Detects the operating system.

    Returns:
        str: The name of the OS in lowercase.
             Returns 'wsl' for Windows Subsystem for Linux,
             'linux' for native Linux,
             or the lowercase name of other platforms (e.g., 'windows', 'darwin').
    """
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/version") as f:
                version_info = f.read().lower()
                if "microsoft" in version_info:
                    return "wsl"
        except Exception:
            pass
        return "linux"
    else:
        return system.lower()


def get_windows_appdata_roaming() -> Optional[Path]:
    """
    Retrieves the Windows 'AppData\\Roaming' directory path from WSL.

    Returns:
        Optional[Path]: A Path object representing the translated Linux-style path
                        to the Windows AppData\\Roaming folder, or None if retrieval fails.
    """
    try:
        output = (
            subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", "[Environment]::GetFolderPath('ApplicationData')"],
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8")
            .strip()
        )
        linux_path = "/mnt/" + output[0].lower() + output[2:].replace("\\", "/")
        return Path(linux_path)
    except Exception:
        return None
