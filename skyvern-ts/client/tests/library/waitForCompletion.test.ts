import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GetRunResponse, WorkflowRunResponse } from "../../src/api/index";
import { SkyvernClient } from "../../src/Client";
import { HttpResponsePromise } from "../../src/core/fetcher/HttpResponsePromise";
import type { RawResponse } from "../../src/core/fetcher/RawResponse";
import { Skyvern } from "../../src/library/Skyvern";
import type { SkyvernBrowser } from "../../src/library/SkyvernBrowser";
import { SkyvernBrowserPageAgent } from "../../src/library/SkyvernBrowserPageAgent";

const RAW_RESPONSE: RawResponse = {
    headers: new Headers(),
    redirected: false,
    status: 200,
    statusText: "OK",
    type: "basic" as ResponseType,
    url: "https://api.skyvern.com/v1/runs/wr_1",
};

function workflowRun(status: string, retryPending = false): GetRunResponse {
    return {
        run_id: "wr_1",
        run_type: "workflow_run",
        status,
        created_at: "2026-10-25T00:00:00Z",
        modified_at: "2026-10-25T00:00:00Z",
        retry_pending: retryPending,
    } as unknown as GetRunResponse;
}

function respondWith(run: GetRunResponse): HttpResponsePromise<GetRunResponse> {
    return HttpResponsePromise.fromResult({ data: run, rawResponse: RAW_RESPONSE });
}

/**
 * A workflow run under a retry policy reports a terminal status with `retry_pending: true`
 * between attempts; attempt 2 then runs and can succeed. Polling past the end of the script is an
 * error so that an over-polling loop fails as loudly as a short-circuiting one.
 */
function scriptRetryPolls(): () => HttpResponsePromise<GetRunResponse> {
    const polls = [workflowRun("failed", true), workflowRun("running"), workflowRun("completed")];
    let index = 0;
    return () => {
        const run = polls[index];
        if (run === undefined) {
            throw new Error(`getRun polled ${index + 1} times, but the run settled on poll ${polls.length}`);
        }
        index += 1;
        return respondWith(run);
    };
}

describe("waitForCompletion with a pending retry", () => {
    beforeEach(() => {
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it("returns the retried run from Skyvern.runWorkflow, not the first attempt's failure", async () => {
        vi.spyOn(SkyvernClient.prototype, "getRun").mockImplementation(scriptRetryPolls());
        vi.spyOn(SkyvernClient.prototype, "runWorkflow").mockReturnValue(
            respondWith(workflowRun("queued")) as unknown as HttpResponsePromise<WorkflowRunResponse>,
        );

        const skyvern = new Skyvern({ apiKey: "test-api-key" });
        const pending = skyvern.runWorkflow({ agent_id: "wpid_1", waitForCompletion: true });
        await vi.advanceTimersByTimeAsync(60_000);

        expect((await pending).status).toBe("completed");
    });

    it("returns the retried run from the page agent's runWorkflow, not the first attempt's failure", async () => {
        const browser = {
            browserSessionId: "pbs_1",
            browserAddress: undefined,
            skyvern: {
                environment: "local",
                getRun: vi.fn(scriptRetryPolls()),
                runWorkflow: () => respondWith(workflowRun("queued")),
            },
        } as unknown as SkyvernBrowser;

        const agent = new SkyvernBrowserPageAgent(browser, { url: () => "about:blank" } as never);
        const pending = agent.runWorkflow("wpid_1");
        await vi.advanceTimersByTimeAsync(60_000);

        expect((await pending).status).toBe("completed");
    });
});
