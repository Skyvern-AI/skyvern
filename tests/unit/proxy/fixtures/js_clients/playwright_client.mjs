// Playwright for JS, driving a page through the proxy. Prints one JSON line for the
// test to assert on. Hermetic: the page is a data: URL, so no network and no site.
import { chromium } from "playwright-core";

const endpoint = process.argv[2];
const browser = await chromium.connectOverCDP(endpoint);
try {
  const context = browser.contexts()[0] ?? (await browser.newContext());
  const page = await context.newPage();
  await page.goto("data:text/html,<title>proxied</title><h1>hello</h1>");
  const title = await page.title();
  const heading = await page.textContent("h1");
  console.log(JSON.stringify({ client: "playwright-js", title, heading }));
} finally {
  await browser.close();
}
