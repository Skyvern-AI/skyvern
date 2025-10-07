import asyncio

from skyvern.forge.sdk.schemas.credentials import NonEmptyPasswordCredential
from skyvern.library.sdk.skyvern_sdk import SkyvernSdk
from skyvern.schemas.run_blocks import CredentialType


async def main() -> None:
    skyvern = SkyvernSdk()

    credentials = await skyvern.client.get_credentials()

    credential = next((item for item in credentials if item.name == "ml_sten1"), None)
    if credential is None:
        credential = await skyvern.client.create_credential(
            name="ml_sten1",
            credential_type="password",
            credential=NonEmptyPasswordCredential(
                username="sten1",
                password="testpass",
                totp=None,
            )
        )

    browser = await skyvern.launch_local_browser()
    page = await browser.get_working_page()

    await page.goto("https://mlgame.us/")

    r = await page.ai.login(
        credential_type=CredentialType.skyvern,
        credential_id=credential.credential_id,
    )
    print(r)

    await page.click("#financeAccountButton")
    await asyncio.sleep(1)
    await page.screenshot(path="screenshot.png", full_page=True)

    await asyncio.sleep(10)


if __name__ == '__main__':
    asyncio.run(main())
