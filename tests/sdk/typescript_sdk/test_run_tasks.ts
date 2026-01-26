import { Skyvern, SkyvernEnvironment } from "@skyvern/client";
import "dotenv/config";

const WEB_SERVER = process.env.WEB_SERVER || "http://localhost:9010";

const skyvern = new Skyvern({
    apiKey: process.env.SKYVERN_API_KEY!,
    environment: SkyvernEnvironment.Local,
});

async function testMlgameLogin() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/login.html`);

    const credentials = await skyvern.getCredentials();
    let credential = credentials.find((item) => item.name === "test_login");

    if (!credential) {
        console.log("Credentials not found. Creating new one.");
        credential = await skyvern.createCredential({
            name: "test_login",
            credential_type: "password",
            credential: {
                username: "testlogin",
                password: "testpassword",
                totp_type: "none",
            },
        });
    }

    await page.agent.login("skyvern", {
        credentialId: credential.credential_id,
    });

    await page.click("#accountBtn");
    await new Promise((resolve) => setTimeout(resolve, 1000));
    await page.act("Click on 'Click Me' button");
    console.assert(await page.locator("#clickCounter").textContent() == "Button clicked 1 times");

    await new Promise((resolve) => setTimeout(resolve, 1000));
    await page.screenshot({ path: "screenshot.png", fullPage: true });

    console.log("All tests passed");
}

async function testFinishesLogin() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto("https://www.saucedemo.com/");
    await page.fill("#user-name", "standard_user");
    await page.fill("#password", "secret_sauce");

    await page.agent.runTask("Click on login button", { engine: "skyvern-1.0" });

    console.assert((await page.getByRole("button", { name: "Add to cart" }).count()) > 0);

    console.log("All tests passed");
}

async function testDownloadFile() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/download_file.html`);

    const result = await page.agent.downloadFiles(
        "Click the 'Download PDF Report' button to download the sample PDF file",
        { downloadSuffix: "sample_report.pdf" }
    );

    console.log(result.downloaded_files);
    console.assert(result.downloaded_files?.length === 1);

    await new Promise((resolve) => setTimeout(resolve, 2000));
    await page.screenshot({ path: "download_test.png", fullPage: true });

    console.log("All tests passed");
}

const tests: Record<string, () => Promise<void>> = {
    testMlgameLogin,
    testFinishesLogin,
    testDownloadFile,
    all: async () => {
        await testMlgameLogin();
        await testFinishesLogin();
        await testDownloadFile();
    },
};

const testName = process.argv[2] || "all";

if (tests[testName]) {
    tests[testName]()
        .catch((error) => {
            console.error("Test failed:", error);
            process.exit(1);
        })
        .finally(async () => {
            await skyvern.close();
        });
} else {
    console.error(`Unknown test: ${testName}`);
    console.error(`Available tests: ${Object.keys(tests).join(", ")}`);
    process.exit(1);
}
