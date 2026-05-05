def main() -> None:
    from skyvern._cli_bootstrap import (  # noqa: PLC0415
        configure_cli_bootstrap_logging,
        raise_unless_missing_optional_dependency,
    )

    configure_cli_bootstrap_logging()
    from skyvern.cli.commands import cli_app  # noqa: PLC0415

    try:
        from skyvern.cli.core.telemetry import register_cli_telemetry_flush  # noqa: PLC0415
    except ImportError as exc:
        raise_unless_missing_optional_dependency(exc, {"posthog"})
    else:
        register_cli_telemetry_flush()

    cli_app()  # type: ignore


if __name__ == "__main__":
    main()
