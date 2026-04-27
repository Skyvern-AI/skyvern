from dotenv import load_dotenv

from skyvern._cli_bootstrap import configure_cli_bootstrap_logging
from skyvern.utils.env_paths import resolve_backend_env_path


def main() -> None:
    configure_cli_bootstrap_logging()
    from . import cli_app  # noqa: PLC0415

    load_dotenv(resolve_backend_env_path())

    from skyvern.cli.core.telemetry import register_cli_telemetry_flush  # noqa: PLC0415

    register_cli_telemetry_flush()

    cli_app()


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    main()
