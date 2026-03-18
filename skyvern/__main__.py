def main() -> None:
    from skyvern._cli_bootstrap import configure_cli_bootstrap_logging  # noqa: PLC0415

    configure_cli_bootstrap_logging()
    from skyvern.cli.commands import cli_app  # noqa: PLC0415

    cli_app()  # type: ignore


if __name__ == "__main__":
    main()
