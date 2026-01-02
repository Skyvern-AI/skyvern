import fs from "fs";
import { join } from "path";
import { Readable } from "stream";
import { toBinaryUploadRequest, type Uploadable } from "../../../src/core/file/index";

describe("toBinaryUploadRequest", () => {
    const TEST_FILE_PATH = join(__dirname, "..", "test-file.txt");

    beforeEach(() => {
        vi.clearAllMocks();
    });

    describe("Buffer input", () => {
        it("should handle Buffer with all metadata", async () => {
            const buffer = Buffer.from("test data");
            const input: Uploadable.WithMetadata = {
                data: buffer,
                filename: "test.txt",
                contentType: "text/plain",
                contentLength: 42,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="test.txt"',
                "Content-Type": "text/plain",
                "Content-Length": "42",
            });
        });

        it("should handle Buffer without metadata", async () => {
            const buffer = Buffer.from("test data");
            const input: Uploadable.WithMetadata = {
                data: buffer,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Length": "9", // buffer.length
            });
        });

        it("should handle Buffer passed directly", async () => {
            const buffer = Buffer.from("test data");

            const result = await toBinaryUploadRequest(buffer);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Length": "9", // buffer.length
            });
        });
    });

    describe("ArrayBuffer input", () => {
        it("should handle ArrayBuffer with metadata", async () => {
            const arrayBuffer = new ArrayBuffer(10);
            const input: Uploadable.WithMetadata = {
                data: arrayBuffer,
                filename: "data.bin",
                contentType: "application/octet-stream",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(arrayBuffer);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="data.bin"',
                "Content-Type": "application/octet-stream",
                "Content-Length": "10", // arrayBuffer.byteLength
            });
        });

        it("should handle ArrayBuffer passed directly", async () => {
            const arrayBuffer = new ArrayBuffer(10);

            const result = await toBinaryUploadRequest(arrayBuffer);

            expect(result.body).toBe(arrayBuffer);
            expect(result.headers).toEqual({
                "Content-Length": "10", // arrayBuffer.byteLength
            });
        });
    });

    describe("Uint8Array input", () => {
        it("should handle Uint8Array with metadata", async () => {
            const uint8Array = new Uint8Array([1, 2, 3, 4, 5]);
            const input: Uploadable.WithMetadata = {
                data: uint8Array,
                filename: "bytes.bin",
                contentType: "application/octet-stream",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(uint8Array);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="bytes.bin"',
                "Content-Type": "application/octet-stream",
                "Content-Length": "5", // uint8Array.byteLength
            });
        });

        it("should handle Uint8Array passed directly", async () => {
            const uint8Array = new Uint8Array([1, 2, 3, 4, 5]);

            const result = await toBinaryUploadRequest(uint8Array);

            expect(result.body).toBe(uint8Array);
            expect(result.headers).toEqual({
                "Content-Length": "5", // uint8Array.byteLength
            });
        });
    });

    describe("Blob input", () => {
        it("should handle Blob with metadata", async () => {
            const blob = new Blob(["test content"], { type: "text/plain" });
            const input: Uploadable.WithMetadata = {
                data: blob,
                filename: "override.txt",
                contentType: "text/html", // Override blob's type
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(blob);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="override.txt"',
                "Content-Type": "text/html", // Should use provided contentType
                "Content-Length": "12", // blob.size
            });
        });

        it("should handle Blob with intrinsic type", async () => {
            const blob = new Blob(["test content"], { type: "application/json" });
            const input: Uploadable.WithMetadata = {
                data: blob,
                filename: "data.json",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(blob);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="data.json"',
                "Content-Type": "application/json", // Should use blob's type
                "Content-Length": "12", // blob.size
            });
        });

        it("should handle Blob passed directly", async () => {
            const blob = new Blob(["test content"], { type: "text/plain" });

            const result = await toBinaryUploadRequest(blob);

            expect(result.body).toBe(blob);
            expect(result.headers).toEqual({
                "Content-Type": "text/plain", // Should use blob's type
                "Content-Length": "12", // blob.size
            });
        });
    });

    describe("File input", () => {
        it("should handle File with metadata", async () => {
            const file = new File(["file content"], "original.txt", { type: "text/plain" });
            const input: Uploadable.WithMetadata = {
                data: file,
                filename: "renamed.txt",
                contentType: "text/html", // Override file's type
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(file);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="renamed.txt"',
                "Content-Type": "text/html", // Should use provided contentType
                "Content-Length": "12", // file.size
            });
        });

        it("should handle File with intrinsic properties", async () => {
            const file = new File(["file content"], "test.json", { type: "application/json" });
            const input: Uploadable.WithMetadata = {
                data: file,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(file);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="test.json"', // Should use file's name
                "Content-Type": "application/json", // Should use file's type
                "Content-Length": "12", // file.size
            });
        });

        it("should handle File passed directly", async () => {
            const file = new File(["file content"], "direct.txt", { type: "text/plain" });

            const result = await toBinaryUploadRequest(file);

            expect(result.body).toBe(file);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="direct.txt"',
                "Content-Type": "text/plain",
                "Content-Length": "12", // file.size
            });
        });
    });

    describe("ReadableStream input", () => {
        it("should handle ReadableStream with metadata", async () => {
            const stream = new ReadableStream({
                start(controller) {
                    controller.enqueue(new TextEncoder().encode("stream data"));
                    controller.close();
                },
            });
            const input: Uploadable.WithMetadata = {
                data: stream,
                filename: "stream.txt",
                contentType: "text/plain",
                contentLength: 100,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(stream);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="stream.txt"',
                "Content-Type": "text/plain",
                "Content-Length": "100", // Should use provided contentLength
            });
        });

        it("should handle ReadableStream without size", async () => {
            const stream = new ReadableStream({
                start(controller) {
                    controller.enqueue(new TextEncoder().encode("stream data"));
                    controller.close();
                },
            });
            const input: Uploadable.WithMetadata = {
                data: stream,
                filename: "stream.txt",
                contentType: "text/plain",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(stream);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="stream.txt"',
                "Content-Type": "text/plain",
                // No Content-Length header since it cannot be determined from ReadableStream
            });
        });

        it("should handle ReadableStream passed directly", async () => {
            const stream = new ReadableStream({
                start(controller) {
                    controller.enqueue(new TextEncoder().encode("stream data"));
                    controller.close();
                },
            });

            const result = await toBinaryUploadRequest(stream);

            expect(result.body).toBe(stream);
            expect(result.headers).toEqual({
                // No headers since no metadata provided and cannot be determined
            });
        });
    });

    describe("Node.js Readable stream input", () => {
        it("should handle Readable stream with metadata", async () => {
            const readable = new Readable({
                read() {
                    this.push("readable data");
                    this.push(null);
                },
            });
            const input: Uploadable.WithMetadata = {
                data: readable,
                filename: "readable.txt",
                contentType: "text/plain",
                contentLength: 50,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(readable);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="readable.txt"',
                "Content-Type": "text/plain",
                "Content-Length": "50", // Should use provided contentLength
            });
        });

        it("should handle Readable stream without size", async () => {
            const readable = new Readable({
                read() {
                    this.push("readable data");
                    this.push(null);
                },
            });
            const input: Uploadable.WithMetadata = {
                data: readable,
                filename: "readable.txt",
                contentType: "text/plain",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(readable);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="readable.txt"',
                "Content-Type": "text/plain",
                // No Content-Length header since it cannot be determined from Readable
            });
        });

        it("should handle Readable stream passed directly", async () => {
            const readable = new Readable({
                read() {
                    this.push("readable data");
                    this.push(null);
                },
            });

            const result = await toBinaryUploadRequest(readable);

            expect(result.body).toBe(readable);
            expect(result.headers).toEqual({
                // No headers since no metadata provided and cannot be determined
            });
        });
    });

    describe("File path input (FromPath type)", () => {
        it("should handle file path with all metadata", async () => {
            const input: Uploadable.FromPath = {
                path: TEST_FILE_PATH,
                filename: "custom.txt",
                contentType: "text/html",
                contentLength: 42,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBeInstanceOf(fs.ReadStream);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="custom.txt"',
                "Content-Type": "text/html",
                "Content-Length": "42", // Should use provided contentLength
            });
        });

        it("should handle file path with minimal metadata", async () => {
            const input: Uploadable.FromPath = {
                path: TEST_FILE_PATH,
                contentType: "text/plain",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBeInstanceOf(fs.ReadStream);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="test-file.txt"', // Should extract from path
                "Content-Type": "text/plain",
                "Content-Length": "21", // Should determine from file system (test file is 21 bytes)
            });
        });

        it("should handle file path with no metadata", async () => {
            const input: Uploadable.FromPath = {
                path: TEST_FILE_PATH,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBeInstanceOf(fs.ReadStream);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="test-file.txt"', // Should extract from path
                "Content-Length": "21", // Should determine from file system (test file is 21 bytes)
            });
        });
    });

    describe("ArrayBufferView input", () => {
        it("should handle ArrayBufferView with metadata", async () => {
            const arrayBuffer = new ArrayBuffer(10);
            const arrayBufferView = new Int8Array(arrayBuffer);
            const input: Uploadable.WithMetadata = {
                data: arrayBufferView,
                filename: "view.bin",
                contentType: "application/octet-stream",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(arrayBufferView);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="view.bin"',
                "Content-Type": "application/octet-stream",
                "Content-Length": "10", // arrayBufferView.byteLength
            });
        });

        it("should handle ArrayBufferView passed directly", async () => {
            const arrayBuffer = new ArrayBuffer(10);
            const arrayBufferView = new Int8Array(arrayBuffer);

            const result = await toBinaryUploadRequest(arrayBufferView);

            expect(result.body).toBe(arrayBufferView);
            expect(result.headers).toEqual({
                "Content-Length": "10", // arrayBufferView.byteLength
            });
        });
    });

    describe("Edge cases", () => {
        it("should handle empty headers when no metadata is available", async () => {
            const buffer = Buffer.from("");
            const input: Uploadable.WithMetadata = {
                data: buffer,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Length": "0",
            });
        });

        it("should handle zero contentLength", async () => {
            const buffer = Buffer.from("test");
            const input: Uploadable.WithMetadata = {
                data: buffer,
                contentLength: 0,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Length": "0", // Should use provided 0
            });
        });

        it("should handle null filename", async () => {
            const buffer = Buffer.from("test");
            const input: Uploadable.WithMetadata = {
                data: buffer,
                filename: undefined,
                contentType: "text/plain",
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Type": "text/plain",
                "Content-Length": "4",
                // No Content-Disposition since filename is undefined
            });
        });

        it("should handle null contentType", async () => {
            const buffer = Buffer.from("test");
            const input: Uploadable.WithMetadata = {
                data: buffer,
                filename: "test.txt",
                contentType: undefined,
            };

            const result = await toBinaryUploadRequest(input);

            expect(result.body).toBe(buffer);
            expect(result.headers).toEqual({
                "Content-Disposition": 'attachment; filename="test.txt"',
                "Content-Length": "4",
                // No Content-Type since contentType is undefined
            });
        });
    });
});
