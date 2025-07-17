import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from skyvern.cli.commands import cli_app

if __name__ == "__main__":
    cli_app()  # type: ignore
