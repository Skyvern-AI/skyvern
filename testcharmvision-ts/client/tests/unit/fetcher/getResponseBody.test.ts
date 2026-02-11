import { getResponseBody } from "../../../src/core/fetcher/getResponseBody";
import { RUNTIME } from "../../../src/core/runtime";

describe("Test getResponseBody", () => {
    it("should handle blob response type", async () => {
        const mockBlob = new Blob(["test"], { type: "text/plain" });
        const mockResponse = new Response(mockBlob);
        const result = await getResponseBody(mockResponse, "blob");
        // @ts-expect-error
        expect(result.constructor.name).toBe("Blob");
    });

    it("should handle sse response type", async () => {
        if (RUNTIME.type === "node") {
            const mockStream = new ReadableStream();
            const mockResponse = new Response(mockStream);
            const result = await getResponseBody(mockResponse, "sse");
            expect(result).toBe(mockStream);
        }
    });

    it("should handle streaming response type", async () => {
        // Create a ReadableStream with some test data
        const encoder = new TextEncoder();
        const testData = "test stream data";
        const mockStream = new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode(testData));
                controller.close();
            },
        });

        const mockResponse = new Response(mockStream);
        const result = (await getResponseBody(mockResponse, "streaming")) as ReadableStream;

        expect(result).toBeInstanceOf(ReadableStream);

        // Read and verify the stream content
        const reader = result.getReader();
        const decoder = new TextDecoder();
        const { value } = await reader.read();
        const streamContent = decoder.decode(value);
        expect(streamContent).toBe(testData);
    });

    it("should handle text response type", async () => {
        const mockResponse = new Response("test text");
        const result = await getResponseBody(mockResponse, "text");
        expect(result).toBe("test text");
    });

    it("should handle JSON response", async () => {
        const mockJson = { key: "value" };
        const mockResponse = new Response(JSON.stringify(mockJson));
        const result = await getResponseBody(mockResponse);
        expect(result).toEqual(mockJson);
    });

    it("should handle empty response", async () => {
        const mockResponse = new Response("");
        const result = await getResponseBody(mockResponse);
        expect(result).toBeUndefined();
    });

    it("should handle non-JSON response", async () => {
        const mockResponse = new Response("invalid json");
        const result = await getResponseBody(mockResponse);
        expect(result).toEqual({
            ok: false,
            error: {
                reason: "non-json",
                statusCode: 200,
                rawBody: "invalid json",
            },
        });
    });
});
