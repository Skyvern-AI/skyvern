def main() -> None:
    from skyvern._cli_bootstrap import configure_cli_bootstrap_logging  # noqa: PLC0415

    configure_cli_bootstrap_logging()
    from skyvern.cli.commands import cli_app  # noqa: PLC0415
    from skyvern.cli.core.telemetry import register_cli_telemetry_flush  # noqa: PLC0415

    register_cli_telemetry_flush()

    cli_app()  # type: ignore


if __name__ == "__main__":
    main()
