import { requestWithRetries } from "../../../src/core/fetcher/requestWithRetries";

describe("requestWithRetries", () => {
    let mockFetch: import("vitest").Mock;
    let originalMathRandom: typeof Math.random;
    let setTimeoutSpy: import("vitest").MockInstance;

    beforeEach(() => {
        mockFetch = vi.fn();
        originalMathRandom = Math.random;

        // Mock Math.random for consistent jitter
        Math.random = vi.fn(() => 0.5);

        vi.useFakeTimers({
            toFake: [
                "setTimeout",
                "clearTimeout",
                "setInterval",
                "clearInterval",
                "setImmediate",
                "clearImmediate",
                "Date",
                "performance",
                "requestAnimationFrame",
                "cancelAnimationFrame",
                "requestIdleCallback",
                "cancelIdleCallback",
            ],
        });
    });

    afterEach(() => {
        Math.random = originalMathRandom;
        vi.clearAllMocks();
        vi.clearAllTimers();
    });

    it("should retry on retryable status codes", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        const retryableStatuses = [408, 429, 500, 502];
        let callCount = 0;

        mockFetch.mockImplementation(async () => {
            if (callCount < retryableStatuses.length) {
                return new Response("", { status: retryableStatuses[callCount++] });
            }
            return new Response("", { status: 200 });
        });

        const responsePromise = requestWithRetries(() => mockFetch(), retryableStatuses.length);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        expect(mockFetch).toHaveBeenCalledTimes(retryableStatuses.length + 1);
        expect(response.status).toBe(200);
    });

    it("should respect maxRetries limit", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        const maxRetries = 2;
        mockFetch.mockResolvedValue(new Response("", { status: 500 }));

        const responsePromise = requestWithRetries(() => mockFetch(), maxRetries);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        expect(mockFetch).toHaveBeenCalledTimes(maxRetries + 1);
        expect(response.status).toBe(500);
    });

    it("should not retry on success status codes", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        const successStatuses = [200, 201, 202];

        for (const status of successStatuses) {
            mockFetch.mockReset();
            setTimeoutSpy.mockClear();
            mockFetch.mockResolvedValueOnce(new Response("", { status }));

            const responsePromise = requestWithRetries(() => mockFetch(), 3);
            await vi.runAllTimersAsync();
            await responsePromise;

            expect(mockFetch).toHaveBeenCalledTimes(1);
            expect(setTimeoutSpy).not.toHaveBeenCalled();
        }
    });

    it("should apply correct exponential backoff with jitter", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        mockFetch.mockResolvedValue(new Response("", { status: 500 }));
        const maxRetries = 3;
        const expectedDelays = [1000, 2000, 4000];

        const responsePromise = requestWithRetries(() => mockFetch(), maxRetries);
        await vi.runAllTimersAsync();
        await responsePromise;

        // Verify setTimeout calls
        expect(setTimeoutSpy).toHaveBeenCalledTimes(expectedDelays.length);

        expectedDelays.forEach((delay, index) => {
            expect(setTimeoutSpy).toHaveBeenNthCalledWith(index + 1, expect.any(Function), delay);
        });

        expect(mockFetch).toHaveBeenCalledTimes(maxRetries + 1);
    });

    it("should handle concurrent retries independently", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        mockFetch
            .mockResolvedValueOnce(new Response("", { status: 500 }))
            .mockResolvedValueOnce(new Response("", { status: 500 }))
            .mockResolvedValueOnce(new Response("", { status: 200 }))
            .mockResolvedValueOnce(new Response("", { status: 200 }));

        const promise1 = requestWithRetries(() => mockFetch(), 1);
        const promise2 = requestWithRetries(() => mockFetch(), 1);

        await vi.runAllTimersAsync();
        const [response1, response2] = await Promise.all([promise1, promise2]);

        expect(response1.status).toBe(200);
        expect(response2.status).toBe(200);
    });

    it("should respect retry-after header with seconds value", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        mockFetch
            .mockResolvedValueOnce(
                new Response("", {
                    status: 429,
                    headers: new Headers({ "retry-after": "5" }),
                }),
            )
            .mockResolvedValueOnce(new Response("", { status: 200 }));

        const responsePromise = requestWithRetries(() => mockFetch(), 1);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), 5000); // 5 seconds = 5000ms
        expect(response.status).toBe(200);
    });

    it("should respect retry-after header with HTTP date value", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        const futureDate = new Date(Date.now() + 3000); // 3 seconds from now
        mockFetch
            .mockResolvedValueOnce(
                new Response("", {
                    status: 429,
                    headers: new Headers({ "retry-after": futureDate.toUTCString() }),
                }),
            )
            .mockResolvedValueOnce(new Response("", { status: 200 }));

        const responsePromise = requestWithRetries(() => mockFetch(), 1);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        // Should use the date-based delay (approximately 3000ms, but with jitter)
        expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), expect.any(Number));
        const actualDelay = setTimeoutSpy.mock.calls[0][1];
        expect(actualDelay).toBeGreaterThan(2000);
        expect(actualDelay).toBeLessThan(4000);
        expect(response.status).toBe(200);
    });

    it("should respect x-ratelimit-reset header", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        const resetTime = Math.floor((Date.now() + 4000) / 1000); // 4 seconds from now in Unix timestamp
        mockFetch
            .mockResolvedValueOnce(
                new Response("", {
                    status: 429,
                    headers: new Headers({ "x-ratelimit-reset": resetTime.toString() }),
                }),
            )
            .mockResolvedValueOnce(new Response("", { status: 200 }));

        const responsePromise = requestWithRetries(() => mockFetch(), 1);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        // Should use the x-ratelimit-reset delay (approximately 4000ms, but with positive jitter)
        expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), expect.any(Number));
        const actualDelay = setTimeoutSpy.mock.calls[0][1];
        expect(actualDelay).toBeGreaterThan(3000);
        expect(actualDelay).toBeLessThan(6000);
        expect(response.status).toBe(200);
    });

    it("should cap delay at MAX_RETRY_DELAY for large header values", async () => {
        setTimeoutSpy = vi.spyOn(global, "setTimeout").mockImplementation((callback: (args: void) => void) => {
            process.nextTick(callback);
            return null as any;
        });

        mockFetch
            .mockResolvedValueOnce(
                new Response("", {
                    status: 429,
                    headers: new Headers({ "retry-after": "120" }), // 120 seconds = 120000ms > MAX_RETRY_DELAY (60000ms)
                }),
            )
            .mockResolvedValueOnce(new Response("", { status: 200 }));

        const responsePromise = requestWithRetries(() => mockFetch(), 1);
        await vi.runAllTimersAsync();
        const response = await responsePromise;

        // Should be capped at MAX_RETRY_DELAY (60000ms) with jitter applied
        expect(setTimeoutSpy).toHaveBeenCalledWith(expect.any(Function), 60000); // Exactly MAX_RETRY_DELAY since jitter with 0.5 random keeps it at 60000
        expect(response.status).toBe(200);
    });
});
