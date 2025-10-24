import fs from "fs";
import { join } from "path";
import stream from "stream";
import type { BinaryResponse } from "../../../src/core";
import { type Fetcher, fetcherImpl } from "../../../src/core/fetcher/Fetcher";

describe("Test fetcherImpl", () => {
    it("should handle successful request", async () => {
        const mockArgs: Fetcher.Args = {
            url: "https://httpbin.org/post",
            method: "POST",
            headers: { "X-Test": "x-test-header" },
            body: { data: "test" },
            contentType: "application/json",
            requestType: "json",
            responseType: "json",
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ data: "test" }), {
                status: 200,
                statusText: "OK",
            }),
        );

        const result = await fetcherImpl(mockArgs);
        expect(result.ok).toBe(true);
        if (result.ok) {
            expect(result.body).toEqual({ data: "test" });
        }

        expect(global.fetch).toHaveBeenCalledWith(
            "https://httpbin.org/post",
            expect.objectContaining({
                method: "POST",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
                body: JSON.stringify({ data: "test" }),
            }),
        );
    });

    it("should send octet stream", async () => {
        const url = "https://httpbin.org/post/file";
        const mockArgs: Fetcher.Args = {
            url,
            method: "POST",
            headers: { "X-Test": "x-test-header" },
            contentType: "application/octet-stream",
            requestType: "bytes",
            responseType: "json",
            body: fs.createReadStream(join(__dirname, "test-file.txt")),
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ data: "test" }), {
                status: 200,
                statusText: "OK",
            }),
        );

        const result = await fetcherImpl(mockArgs);

        expect(global.fetch).toHaveBeenCalledWith(
            url,
            expect.objectContaining({
                method: "POST",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
                body: expect.any(fs.ReadStream),
            }),
        );
        expect(result.ok).toBe(true);
        if (result.ok) {
            expect(result.body).toEqual({ data: "test" });
        }
    });

    it("should receive file as stream", async () => {
        const url = "https://httpbin.org/post/file";
        const mockArgs: Fetcher.Args = {
            url,
            method: "GET",
            headers: { "X-Test": "x-test-header" },
            responseType: "binary-response",
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(
                stream.Readable.toWeb(fs.createReadStream(join(__dirname, "test-file.txt"))) as ReadableStream,
                {
                    status: 200,
                    statusText: "OK",
                },
            ),
        );

        const result = await fetcherImpl(mockArgs);

        expect(global.fetch).toHaveBeenCalledWith(
            url,
            expect.objectContaining({
                method: "GET",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
            }),
        );
        expect(result.ok).toBe(true);
        if (result.ok) {
            const body = result.body as BinaryResponse;
            expect(body).toBeDefined();
            expect(body.bodyUsed).toBe(false);
            expect(typeof body.stream).toBe("function");
            const stream = body.stream();
            expect(stream).toBeInstanceOf(ReadableStream);
            const reader = stream.getReader();
            const { value } = await reader.read();
            const decoder = new TextDecoder();
            const streamContent = decoder.decode(value);
            expect(streamContent).toBe("This is a test file!\n");
            expect(body.bodyUsed).toBe(true);
        }
    });

    it("should receive file as blob", async () => {
        const url = "https://httpbin.org/post/file";
        const mockArgs: Fetcher.Args = {
            url,
            method: "GET",
            headers: { "X-Test": "x-test-header" },
            responseType: "binary-response",
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(
                stream.Readable.toWeb(fs.createReadStream(join(__dirname, "test-file.txt"))) as ReadableStream,
                {
                    status: 200,
                    statusText: "OK",
                },
            ),
        );

        const result = await fetcherImpl(mockArgs);

        expect(global.fetch).toHaveBeenCalledWith(
            url,
            expect.objectContaining({
                method: "GET",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
            }),
        );
        expect(result.ok).toBe(true);
        if (result.ok) {
            const body = result.body as BinaryResponse;
            expect(body).toBeDefined();
            expect(body.bodyUsed).toBe(false);
            expect(typeof body.blob).toBe("function");
            const blob = await body.blob();
            expect(blob).toBeInstanceOf(Blob);
            const reader = blob.stream().getReader();
            const { value } = await reader.read();
            const decoder = new TextDecoder();
            const streamContent = decoder.decode(value);
            expect(streamContent).toBe("This is a test file!\n");
            expect(body.bodyUsed).toBe(true);
        }
    });

    it("should receive file as arraybuffer", async () => {
        const url = "https://httpbin.org/post/file";
        const mockArgs: Fetcher.Args = {
            url,
            method: "GET",
            headers: { "X-Test": "x-test-header" },
            responseType: "binary-response",
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(
                stream.Readable.toWeb(fs.createReadStream(join(__dirname, "test-file.txt"))) as ReadableStream,
                {
                    status: 200,
                    statusText: "OK",
                },
            ),
        );

        const result = await fetcherImpl(mockArgs);

        expect(global.fetch).toHaveBeenCalledWith(
            url,
            expect.objectContaining({
                method: "GET",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
            }),
        );
        expect(result.ok).toBe(true);
        if (result.ok) {
            const body = result.body as BinaryResponse;
            expect(body).toBeDefined();
            expect(body.bodyUsed).toBe(false);
            expect(typeof body.arrayBuffer).toBe("function");
            const arrayBuffer = await body.arrayBuffer();
            expect(arrayBuffer).toBeInstanceOf(ArrayBuffer);
            const decoder = new TextDecoder();
            const streamContent = decoder.decode(new Uint8Array(arrayBuffer));
            expect(streamContent).toBe("This is a test file!\n");
            expect(body.bodyUsed).toBe(true);
        }
    });

    it("should receive file as bytes", async () => {
        const url = "https://httpbin.org/post/file";
        const mockArgs: Fetcher.Args = {
            url,
            method: "GET",
            headers: { "X-Test": "x-test-header" },
            responseType: "binary-response",
        };

        global.fetch = vi.fn().mockResolvedValue(
            new Response(
                stream.Readable.toWeb(fs.createReadStream(join(__dirname, "test-file.txt"))) as ReadableStream,
                {
                    status: 200,
                    statusText: "OK",
                },
            ),
        );

        const result = await fetcherImpl(mockArgs);

        expect(global.fetch).toHaveBeenCalledWith(
            url,
            expect.objectContaining({
                method: "GET",
                headers: expect.objectContaining({ "X-Test": "x-test-header" }),
            }),
        );
        expect(result.ok).toBe(true);
        if (result.ok) {
            const body = result.body as BinaryResponse;
            expect(body).toBeDefined();
            expect(body.bodyUsed).toBe(false);
            expect(typeof body.bytes).toBe("function");
            if (!body.bytes) {
                return;
            }
            const bytes = await body.bytes();
            expect(bytes).toBeInstanceOf(Uint8Array);
            const decoder = new TextDecoder();
            const streamContent = decoder.decode(bytes);
            expect(streamContent).toBe("This is a test file!\n");
            expect(body.bodyUsed).toBe(true);
        }
    });
});
