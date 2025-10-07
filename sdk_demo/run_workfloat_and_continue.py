import asyncio

from skyvern.library.sdk.skyvern_sdk import SkyvernSdk
from skyvern.schemas.runs import RunEngine


async def main() -> None:
    skyvern = SkyvernSdk()

    browser = await skyvern.launch_local_browser()
    page = await browser.get_working_page()

    print(f"Running AI workflow...")
    r = await page.ai.run_workflow(workflow_id="wpid_447094269942327664")
    print(f"AI login result: {r}")

    await page.click("#sendToAdminButton")
    await asyncio.sleep(1)
    await page.screenshot(path="screenshot.png", full_page=True)



if __name__ == '__main__':
    asyncio.run(main())
