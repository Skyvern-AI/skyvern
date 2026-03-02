from dotenv import load_dotenv

from skyvern.utils.env_paths import resolve_backend_env_path

from . import cli_app

if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv(resolve_backend_env_path())
    cli_app()
