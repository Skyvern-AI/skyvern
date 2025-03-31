from alembic import command
from alembic.config import Config
from skyvern.constants import REPO_ROOT_DIR
import subprocess
import platform
from pathlib import Path


def migrate_db() -> None:
    alembic_cfg = Config()
    path = f"{REPO_ROOT_DIR}/alembic"
    alembic_cfg.set_main_option("script_location", path)
    command.upgrade(alembic_cfg, "head")

def detect_os():
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/version", "r") as f:
                version_info = f.read().lower()
                if "microsoft" in version_info:
                        return "wsl"
        except Exception:
            pass
        return "linux"
    else:
        return system.lower()
    
def get_windows_appdata_roaming():
    try:
        output = subprocess.check_output([
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Environment]::GetFolderPath('ApplicationData')"
        ], stderr=subprocess.DEVNULL).decode('utf-8').strip()
        linux_path = "/mnt/" + output[0].lower() + output[2:].replace("\\", "/")
        return Path(linux_path)
    except Exception as e:
        return None
