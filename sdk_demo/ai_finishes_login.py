import asyncio

from skyvern.library.sdk.skyvern_sdk import SkyvernSdk
from skyvern.schemas.runs import RunEngine


async def main() -> None:
    skyvern = SkyvernSdk()

    browser = await skyvern.launch_local_browser()
    page = await browser.get_working_page()

    await page.goto("https://mlgame.us")
    await page.fill("#username", "sten1")
    await page.fill("#password", "testpass")

    r = await page.ai.run_task(prompt="Click on 'Login' button",
                               engine=RunEngine.skyvern_v1)
    print(f"AI login result: {r}")


if __name__ == '__main__':
    asyncio.run(main())
