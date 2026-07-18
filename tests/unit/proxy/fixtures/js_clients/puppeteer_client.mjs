// Puppeteer, driving a page through the proxy. Prints one JSON line to assert on.
//
// A ws:// endpoint connects via browserWSEndpoint, an http:// one via browserURL —
// the test drives both, because only the first can address a path-scoped session:
// browserURL resolves discovery as `new URL('/json/version', browserURL)`
// (BrowserConnector.js), an ABSOLUTE path that discards the session prefix.
import puppeteer from "puppeteer-core";

const endpoint = process.argv[2];
const connectOptions = endpoint.startsWith("ws")
  ? { browserWSEndpoint: endpoint }
  : { browserURL: endpoint };

const browser = await puppeteer.connect(connectOptions);
try {
  const page = await browser.newPage();
  await page.goto("data:text/html,<title>proxied</title><h1>hello</h1>");
  const title = await page.title();
  const heading = await page.$eval("h1", (node) => node.textContent);
  console.log(JSON.stringify({ client: "puppeteer", title, heading }));
} finally {
  await browser.disconnect();
}
