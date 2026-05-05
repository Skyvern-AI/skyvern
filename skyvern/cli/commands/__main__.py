from dotenv import load_dotenv

from skyvern._cli_bootstrap import configure_cli_bootstrap_logging, raise_unless_missing_optional_dependency
from skyvern.utils.env_paths import resolve_backend_env_path


def main() -> None:
    configure_cli_bootstrap_logging()
    from . import cli_app  # noqa: PLC0415

    load_dotenv(resolve_backend_env_path())

    try:
        from skyvern.cli.core.telemetry import register_cli_telemetry_flush  # noqa: PLC0415
    except ImportError as exc:
        raise_unless_missing_optional_dependency(exc, {"posthog"})
    else:
        register_cli_telemetry_flush()

    cli_app()


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    main()
