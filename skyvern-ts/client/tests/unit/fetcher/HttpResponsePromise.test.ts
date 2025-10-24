import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpResponsePromise } from "../../../src/core/fetcher/HttpResponsePromise";
import type { RawResponse, WithRawResponse } from "../../../src/core/fetcher/RawResponse";

describe("HttpResponsePromise", () => {
    const mockRawResponse: RawResponse = {
        headers: new Headers(),
        redirected: false,
        status: 200,
        statusText: "OK",
        type: "basic" as ResponseType,
        url: "https://example.com",
    };
    const mockData = { id: "123", name: "test" };
    const mockWithRawResponse: WithRawResponse<typeof mockData> = {
        data: mockData,
        rawResponse: mockRawResponse,
    };

    describe("fromFunction", () => {
        it("should create an HttpResponsePromise from a function", async () => {
            const mockFn = vi
                .fn<(arg1: string, arg2: string) => Promise<WithRawResponse<typeof mockData>>>()
                .mockResolvedValue(mockWithRawResponse);

            const responsePromise = HttpResponsePromise.fromFunction(mockFn, "arg1", "arg2");

            const result = await responsePromise;
            expect(result).toEqual(mockData);
            expect(mockFn).toHaveBeenCalledWith("arg1", "arg2");

            const resultWithRawResponse = await responsePromise.withRawResponse();
            expect(resultWithRawResponse).toEqual({
                data: mockData,
                rawResponse: mockRawResponse,
            });
        });
    });

    describe("fromPromise", () => {
        it("should create an HttpResponsePromise from a promise", async () => {
            const promise = Promise.resolve(mockWithRawResponse);

            const responsePromise = HttpResponsePromise.fromPromise(promise);

            const result = await responsePromise;
            expect(result).toEqual(mockData);

            const resultWithRawResponse = await responsePromise.withRawResponse();
            expect(resultWithRawResponse).toEqual({
                data: mockData,
                rawResponse: mockRawResponse,
            });
        });
    });

    describe("fromExecutor", () => {
        it("should create an HttpResponsePromise from an executor function", async () => {
            const responsePromise = HttpResponsePromise.fromExecutor((resolve) => {
                resolve(mockWithRawResponse);
            });

            const result = await responsePromise;
            expect(result).toEqual(mockData);

            const resultWithRawResponse = await responsePromise.withRawResponse();
            expect(resultWithRawResponse).toEqual({
                data: mockData,
                rawResponse: mockRawResponse,
            });
        });
    });

    describe("fromResult", () => {
        it("should create an HttpResponsePromise from a result", async () => {
            const responsePromise = HttpResponsePromise.fromResult(mockWithRawResponse);

            const result = await responsePromise;
            expect(result).toEqual(mockData);

            const resultWithRawResponse = await responsePromise.withRawResponse();
            expect(resultWithRawResponse).toEqual({
                data: mockData,
                rawResponse: mockRawResponse,
            });
        });
    });

    describe("Promise methods", () => {
        let responsePromise: HttpResponsePromise<typeof mockData>;

        beforeEach(() => {
            responsePromise = HttpResponsePromise.fromResult(mockWithRawResponse);
        });

        it("should support then() method", async () => {
            const result = await responsePromise.then((data) => ({
                ...data,
                modified: true,
            }));

            expect(result).toEqual({
                ...mockData,
                modified: true,
            });
        });

        it("should support catch() method", async () => {
            const errorResponsePromise = HttpResponsePromise.fromExecutor((_, reject) => {
                reject(new Error("Test error"));
            });

            const catchSpy = vi.fn();
            await errorResponsePromise.catch(catchSpy);

            expect(catchSpy).toHaveBeenCalled();
            const error = catchSpy.mock.calls[0]?.[0];
            expect(error).toBeInstanceOf(Error);
            expect((error as Error).message).toBe("Test error");
        });

        it("should support finally() method", async () => {
            const finallySpy = vi.fn();
            await responsePromise.finally(finallySpy);

            expect(finallySpy).toHaveBeenCalled();
        });
    });

    describe("withRawResponse", () => {
        it("should return both data and raw response", async () => {
            const responsePromise = HttpResponsePromise.fromResult(mockWithRawResponse);

            const result = await responsePromise.withRawResponse();

            expect(result).toEqual({
                data: mockData,
                rawResponse: mockRawResponse,
            });
        });
    });
});
