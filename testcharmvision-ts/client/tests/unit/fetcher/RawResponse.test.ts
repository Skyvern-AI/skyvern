import { describe, expect, it } from "vitest";

import { toRawResponse } from "../../../src/core/fetcher/RawResponse";

describe("RawResponse", () => {
    describe("toRawResponse", () => {
        it("should convert Response to RawResponse by removing body, bodyUsed, and ok properties", () => {
            const mockHeaders = new Headers({ "content-type": "application/json" });
            const mockResponse = {
                body: "test body",
                bodyUsed: false,
                ok: true,
                headers: mockHeaders,
                redirected: false,
                status: 200,
                statusText: "OK",
                type: "basic" as ResponseType,
                url: "https://example.com",
            };

            const result = toRawResponse(mockResponse as unknown as Response);

            expect("body" in result).toBe(false);
            expect("bodyUsed" in result).toBe(false);
            expect("ok" in result).toBe(false);
            expect(result.headers).toBe(mockHeaders);
            expect(result.redirected).toBe(false);
            expect(result.status).toBe(200);
            expect(result.statusText).toBe("OK");
            expect(result.type).toBe("basic");
            expect(result.url).toBe("https://example.com");
        });
    });
});
