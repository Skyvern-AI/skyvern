import { afterEach, describe, expect, it, vi } from "vitest";
import { Skyvern } from "../../src/library/Skyvern.js";

afterEach(() => vi.unstubAllGlobals());

describe("per-run scrape screenshot limits", () => {
    it.each(["task", "workflow"] as const)("sends the limit for a %s run", async (kind) => {
        const fetch = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ run_id: "run_test", status: "created" }), {
                status: 200,
                headers: { "content-type": "application/json" },
            }),
        );
        vi.stubGlobal("fetch", fetch);
        const client = new Skyvern({ apiKey: "test", baseUrl: "https://example.test" });
        if (kind === "task") {
            await client.runTask({ prompt: "Extract the price", max_scrape_screenshots: 1 });
        } else {
            await client.runWorkflow({ agent_id: "wpid_test", max_scrape_screenshots: 1 });
        }
        const body = JSON.parse(fetch.mock.calls[0][1].body);
        expect(body.max_scrape_screenshots).toBe(1);
        expect(body).not.toHaveProperty("max_screenshot_scrolls");
    });
});
