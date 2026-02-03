import asyncio

import pytest

from skyvern.forge.sdk.schemas.credentials import NonEmptyPasswordCredential, TotpType
from skyvern.schemas.run_blocks import CredentialType


@pytest.mark.asyncio
async def test_login(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/login.html")

    credentials = await skyvern_browser.skyvern.get_credentials()
    credential = next((item for item in credentials if item.name == "test_login"), None)
    if credential is None:
        print("Credentials not found. Creating new one.")
        credential = await skyvern_browser.skyvern.create_credential(
            name="test_login",
            credential_type="password",
            credential=NonEmptyPasswordCredential(
                username="testlogin",
                password="testpassword",
                totp=None,
                totp_type=TotpType.NONE,
            ),
        )

    await page.agent.login(
        credential_type=CredentialType.skyvern,
        credential_id=credential.credential_id,
    )

    await page.click("#accountBtn")
    await asyncio.sleep(1)
    await page.act("Click on 'Click Me' button")
    assert await page.locator("#clickCounter").text_content() == "Button clicked 1 times"

    await asyncio.sleep(1)
    await page.screenshot(path="screenshot.png", full_page=True)


@pytest.mark.asyncio
async def test_test_finishes_login(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto("https://www.saucedemo.com/")
    await page.fill("#user-name", "standard_user")
    await page.fill("#password", "secret_sauce")

    await page.agent.run_task("Click on login button", engine="skyvern-1.0")

    assert await page.get_by_role("button", name="Add to cart").count() > 0


@pytest.mark.asyncio
async def test_download_file(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/download_file.html")

    r = await page.agent.download_files(
        prompt="Click the 'Download PDF Report' button to download the sample PDF file",
        download_suffix="sample_report.pdf",
    )
    for downloaded_file in r.downloaded_files:
        print(downloaded_file)
    assert len(r.downloaded_files) == 1

    await asyncio.sleep(2)
    await page.screenshot(path="download_test.png", full_page=True)

    assert len(r.downloaded_files) == 1
