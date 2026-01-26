import { Skyvern, SkyvernEnvironment } from "@skyvern/client";
import "dotenv/config";

const WEB_SERVER = process.env.WEB_SERVER || "http://localhost:9010";

const skyvern = new Skyvern({
    apiKey: process.env.SKYVERN_API_KEY!,
    environment: SkyvernEnvironment.Local,
});

async function testClicks() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/click.html`);

    console.assert((await page.locator("#counter").textContent()) === "Button clicked 0 times");
    await page.click("#button");
    console.assert((await page.locator("#counter").textContent()) === "Button clicked 1 times");

    await page.click({ prompt: "Click on the button" });
    console.assert((await page.locator("#counter").textContent()) === "Button clicked 2 times");

    console.log("Fallback");
    await page.click("#broken-selector", { prompt: "Click on the button" });
    console.assert((await page.locator("#counter").textContent()) === "Button clicked 3 times");

    await page.click("#button");
    console.assert((await page.locator("#counter").textContent()) === "Button clicked 4 times");

    console.log("All tests passed");
}

async function testFill() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/input.html`);

    console.assert((await page.locator("#output").textContent()) === "");
    await page.fill("#nameInput", "Test1");
    await page.click("#submitBtn");
    console.assert((await page.locator("#output").textContent()) === "Hello, Test1!");

    await page.fill({ prompt: "Type 'Test2' in the name input" });
    await page.click("#submitBtn");
    console.assert((await page.locator("#output").textContent()) === "Hello, Test2!");

    await page.fill({ prompt: "Type the value in the name input", value: "Test3" });
    await page.click("#submitBtn");
    console.assert((await page.locator("#output").textContent()) === "Hello, Test3!");

    await page.fill("#nameInputBroken", "TestFallback", { prompt: "Fill the name input" })
    await page.click("#submitBtn")
    console.assert(await page.locator("#output").textContent() == "Hello, TestFallback!")

    console.log("All tests passed");
}

async function testSelectOption() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/combobox.html`);

    await page.selectOption("#cars", "audi");
    console.assert((await page.locator("#cars").inputValue()) === "audi");

    await page.selectOption("#cars", "opel");
    console.assert((await page.locator("#cars").inputValue()) === "opel");

    await page.selectOption("#cars", { label: "Saab" });
    console.assert((await page.locator("#cars").inputValue()) === "saab");

    await page.selectOption({ prompt: "Select 'Audi' i the car combobox" });
    console.assert((await page.locator("#cars").inputValue()) === "audi");

    // fallback
    await page.selectOption("#cars-broken", "opel", { prompt: "Select 'Opel' i the car combobox" })
    console.assert(await page.locator("#cars").inputValue() == "opel")

    console.log("All tests passed");
}

async function testActCombobox() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/combobox.html`);

    await page.act("select 'Audi' from the combobox");

    console.assert((await page.locator("#cars").inputValue()) === "audi");

    console.log("All tests passed");
}

async function testActInputAndClick() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/input.html`);

    await page.act("type 'ActTest' into the input box");
    await page.act("click on the button");

    console.assert((await page.locator("#output").textContent()) === "Hello, ActTest!");

    console.log("All tests passed");
}

async function testExtract() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/click.html`);

    const r1 = await page.extract({ prompt: "give one sentence description of this page" });
    console.log(r1);

    const r2 = await page.extract({
        prompt: "Describe this page",
        schema: {
            type: "object",
            properties: {
                short: { type: "string", description: "one sentence description of this page" },
                long: { type: "string", description: "two-three sentence description of this page" },
            },
            required: ["short", "long"],
        },
    });
    console.log(r2);

    console.log("All tests passed");
}

async function testValidate() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    await page.goto(`${WEB_SERVER}/click.html`);
    await page.click("#button");

    console.assert((await page.validate("if clicked time > 0")) === true);
    console.assert((await page.validate("if clicked time > 1")) === false);
    console.assert((await page.validate("the input text is valid")) === false);

    console.log("All tests passed");
}

async function testPrompt() {
    const browser = await skyvern.connectToBrowserOverCdp("http://localhost:9222");
    const page = await browser.getWorkingPage();

    const r1 = await page.prompt("1111+1111");
    console.log(r1);
    console.assert(String(r1).includes("2222"));

    const r2 = await page.prompt("2+2", {
        type: "object",
        properties: {
            result_number: { type: "number" },
            confidence: { type: "number", minimum: 0, maximum: 1 },
        },
    });
    console.log(r2);
    console.assert((r2 as any).result_number === 4);

    console.log("All tests passed");
}

const tests: Record<string, () => Promise<void>> = {
    testClicks,
    testFill,
    testSelectOption,
    testActCombobox,
    testActInputAndClick,
    testExtract,
    testValidate,
    testPrompt,
    all: async () => {
        await testClicks();
        await testFill();
        await testSelectOption();
        await testActCombobox();
        await testActInputAndClick();
        await testExtract();
        await testValidate();
        await testPrompt();
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
