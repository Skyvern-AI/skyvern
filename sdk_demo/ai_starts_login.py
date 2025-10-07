import asyncio

from skyvern.library.sdk.skyvern_sdk import SkyvernSdk
from skyvern.schemas.runs import RunEngine


async def main() -> None:
    skyvern = SkyvernSdk()

    browser = await skyvern.launch_local_browser()
    page = await browser.get_working_page()

    print(f"Running AI task...")
    r = await page.ai.run_task(prompt="Open https://mlgame.us and fill 'sten1' in Login input. ONLY FILL and terminate",
                               engine=RunEngine.skyvern_v1)
    print(f"AI login result: {r}")

    await asyncio.sleep(2)

    print(f"Continue...")
    await page.fill("#password", "testpass")
    await page.click("#loginButton")

    await asyncio.sleep(20)


if __name__ == '__main__':
    asyncio.run(main())
