import type { Page } from "playwright";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SkyvernApi from "../../src/api/index.js";
import { Skyvern } from "../../src/library/Skyvern.js";
import type { SkyvernBrowser } from "../../src/library/SkyvernBrowser.js";
import { SkyvernBrowserPageAgent } from "../../src/library/SkyvernBrowserPageAgent.js";

type SkyvernInternals = {
    _waitForRunCompletion: (runId: string, timeout: number) => Promise<SkyvernApi.GetRunResponse>;
};

type AgentInternals = {
    _waitForRunCompletion: (runId: string, timeout: number) => Promise<SkyvernApi.GetRunResponse>;
};

describe("waitForCompletion", () => {
    beforeEach(() => {
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.useRealTimers();
    });

    it("keeps polling when status is failed but retry_pending is true in Skyvern", async () => {
        const skyvern = new Skyvern({ apiKey: "test-api-key" });
        const failedPendingRun: SkyvernApi.GetRunResponse.WorkflowRun = {
            run_id: "run_123",
            run_type: "workflow_run",
            status: "failed",
            retry_pending: true,
            created_at: "2026-01-01T00:00:00Z",
            modified_at: "2026-01-01T00:00:00Z",
        };
        const completedRun: SkyvernApi.GetRunResponse.WorkflowRun = {
            run_id: "run_123",
            run_type: "workflow_run",
            status: "completed",
            retry_pending: false,
            created_at: "2026-01-01T00:00:00Z",
            modified_at: "2026-01-01T00:00:00Z",
        };

        const getRunSpy = vi
            .spyOn(skyvern, "getRun")
            .mockResolvedValueOnce(failedPendingRun)
            .mockResolvedValueOnce(completedRun);

        const completionPromise = (skyvern as unknown as SkyvernInternals)._waitForRunCompletion("run_123", 60);

        await vi.advanceTimersByTimeAsync(10000);
        const result = await completionPromise;

        expect(getRunSpy).toHaveBeenCalledTimes(2);
        expect(result.status).toBe("completed");
        expect(result.retry_pending).toBe(false);
    });

    it("keeps polling when status is failed but retry_pending is true in SkyvernBrowserPageAgent", async () => {
        const getRunMock = vi.fn();
        const mockBrowser = {
            skyvern: {
                getRun: getRunMock,
            },
        } as unknown as SkyvernBrowser;
        const mockPage = {
            url: () => "https://example.com",
        } as unknown as Page;

        const agent = new SkyvernBrowserPageAgent(mockBrowser, mockPage);
        const failedPendingRun: SkyvernApi.GetRunResponse.TaskV1 = {
            run_id: "run_456",
            run_type: "task_v1",
            status: "failed",
            retry_pending: true,
            created_at: "2026-01-01T00:00:00Z",
            modified_at: "2026-01-01T00:00:00Z",
        };
        const completedRun: SkyvernApi.GetRunResponse.TaskV1 = {
            run_id: "run_456",
            run_type: "task_v1",
            status: "completed",
            retry_pending: false,
            created_at: "2026-01-01T00:00:00Z",
            modified_at: "2026-01-01T00:00:00Z",
        };

        getRunMock.mockResolvedValueOnce(failedPendingRun).mockResolvedValueOnce(completedRun);

        const completionPromise = (agent as unknown as AgentInternals)._waitForRunCompletion("run_456", 60);

        await vi.advanceTimersByTimeAsync(10000);
        const result = await completionPromise;

        expect(getRunMock).toHaveBeenCalledTimes(2);
        expect(result.status).toBe("completed");
        expect(result.retry_pending).toBe(false);
    });

    it("returns immediately on failed status when retry_pending is false or absent", async () => {
        const skyvern = new Skyvern({ apiKey: "test-api-key" });
        const failedFinalRun: SkyvernApi.GetRunResponse.WorkflowRun = {
            run_id: "run_789",
            run_type: "workflow_run",
            status: "failed",
            retry_pending: false,
            created_at: "2026-01-01T00:00:00Z",
            modified_at: "2026-01-01T00:00:00Z",
        };

        const getRunSpy = vi.spyOn(skyvern, "getRun").mockResolvedValueOnce(failedFinalRun);

        const result = await (skyvern as unknown as SkyvernInternals)._waitForRunCompletion("run_789", 60);

        expect(getRunSpy).toHaveBeenCalledTimes(1);
        expect(result.status).toBe("failed");
        expect(result.retry_pending).toBe(false);
    });
});
