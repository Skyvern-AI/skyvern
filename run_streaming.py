import asyncio
import subprocess

import structlog
import typer

from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir

INTERVAL = 1
LOG = structlog.get_logger()


async def run() -> None:
    file_name = "skyvern_screenshot.png"
    png_file_path = f"{get_skyvern_temp_dir()}/{file_name}"

    while True:
        # run subprocess to take screenshot
        subprocess.run(
            f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {png_file_path}", shell=True, env={"DISPLAY": ":99"}
        )

        # upload screenshot to S3
        try:
            await app.STORAGE.save_streaming_file("placeholder_org", file_name)
        except Exception:
            LOG.info("Failed to save screenshot")

        await asyncio.sleep(INTERVAL)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    typer.run(main)
