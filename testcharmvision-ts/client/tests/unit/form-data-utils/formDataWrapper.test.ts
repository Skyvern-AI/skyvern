import { Blob, File } from "buffer";
import { join } from "path";
/* eslint-disable @typescript-eslint/ban-ts-comment */
import { Readable } from "stream";
import { FormDataWrapper, newFormData } from "../../../src/core/form-data-utils/FormDataWrapper";

// Helper function to serialize FormData to string for inspection
async function serializeFormData(formData: FormData): Promise<string> {
    const request = new Request("http://localhost", {
        method: "POST",
        body: formData,
    });

    const buffer = await request.arrayBuffer();
    return new TextDecoder().decode(buffer);
}

describe("FormDataWrapper", () => {
    let formData: FormDataWrapper;

    beforeEach(async () => {
        formData = new FormDataWrapper();
        await formData.setup();
    });

    it("Upload file by path", async () => {
        await formData.appendFile("file", {
            path: join(__dirname, "..", "test-file.txt"),
        });

        const serialized = await serializeFormData(formData.getRequest().body);

        expect(serialized).toContain('Content-Disposition: form-data; name="file"');
        expect(serialized).toContain('filename="test-file.txt"');
        expect(serialized).toContain("This is a test file!");
    });

    it("Upload file by path with filename", async () => {
        await formData.appendFile("file", {
            path: join(__dirname, "..", "test-file.txt"),
            filename: "custom-file.txt",
        });

        const serialized = await serializeFormData(formData.getRequest().body);

        expect(serialized).toContain('Content-Disposition: form-data; name="file"');
        expect(serialized).toContain('filename="custom-file.txt"');
        expect(serialized).toContain("This is a test file!");
    });

    describe("Stream handling", () => {
        it("serializes Node.js Readable stream with filename", async () => {
            const stream = Readable.from(["file content"]);
            await formData.appendFile("file", {
                data: stream,
                filename: "testfile.txt",
            });

            const serialized = await serializeFormData(formData.getRequest().body);

            expect(serialized).toContain('Content-Disposition: form-data; name="file"');
            expect(serialized).toContain('filename="testfile.txt"');
            expect(serialized).toContain("file content");
        });

        it("auto-detects filename from stream path property", async () => {
            const stream = Readable.from(["file content"]);
            (stream as { path?: string }).path = "/test/path/testfile.txt";

            await formData.appendFile("file", stream);

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="testfile.txt"');
        });

        it("handles Windows-style paths", async () => {
            const stream = Readable.from(["file content"]);
            (stream as { path?: string }).path = "C:\\test\\path\\testfile.txt";

            await formData.appendFile("file", stream);

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="testfile.txt"');
        });

        it("handles empty streams", async () => {
            const stream = Readable.from([]);
            await formData.appendFile("file", {
                data: stream,
                filename: "empty.txt",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="empty.txt"');
            expect(serialized).toMatch(/------formdata-undici-\w+|------WebKitFormBoundary\w+/);
        });

        it("serializes Web ReadableStream with filename", async () => {
            const stream = new ReadableStream({
                start(controller) {
                    controller.enqueue(new TextEncoder().encode("web stream content"));
                    controller.close();
                },
            });

            await formData.appendFile("file", {
                data: stream,
                filename: "webstream.txt",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="webstream.txt"');
            expect(serialized).toContain("web stream content");
        });

        it("handles empty Web ReadableStream", async () => {
            const stream = new ReadableStream({
                start(controller) {
                    controller.close();
                },
            });

            await formData.appendFile("file", {
                data: stream,
                filename: "empty.txt",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="empty.txt"');
            expect(serialized).toMatch(/------formdata-undici-\w+|------WebKitFormBoundary\w+/);
        });
    });

    describe("Blob and File types", () => {
        it("serializes Blob with specified filename", async () => {
            const blob = new Blob(["file content"], { type: "text/plain" });
            await formData.appendFile("file", {
                data: blob,
                filename: "testfile.txt",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="testfile.txt"');
            expect(serialized).toContain("Content-Type: text/plain");
            expect(serialized).toContain("file content");
        });

        it("uses default filename for Blob without explicit filename", async () => {
            const blob = new Blob(["file content"], { type: "text/plain" });
            await formData.appendFile("file", blob);

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="blob"');
        });

        it("preserves File object filename", async () => {
            if (typeof File !== "undefined") {
                const file = new File(["file content"], "original.txt", { type: "text/plain" });
                await formData.appendFile("file", file);

                const serialized = await serializeFormData(formData.getRequest().body);
                expect(serialized).toContain('filename="original.txt"');
                expect(serialized).toContain("file content");
            }
        });

        it("allows filename override for File objects", async () => {
            if (typeof File !== "undefined") {
                const file = new File(["file content"], "original.txt", { type: "text/plain" });
                await formData.appendFile("file", {
                    data: file,
                    filename: "override.txt",
                });

                const serialized = await serializeFormData(formData.getRequest().body);
                expect(serialized).toContain('filename="override.txt"');
                expect(serialized).not.toContain('filename="original.txt"');
            }
        });
    });

    describe("Binary data types", () => {
        it("serializes ArrayBuffer with filename", async () => {
            const arrayBuffer = new ArrayBuffer(8);
            new Uint8Array(arrayBuffer).set([1, 2, 3, 4, 5, 6, 7, 8]);

            await formData.appendFile("file", {
                data: arrayBuffer,
                filename: "binary.bin",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="binary.bin"');
            expect(serialized).toMatch(/------formdata-undici-\w+|------WebKitFormBoundary\w+/);
        });

        it("serializes Uint8Array with filename", async () => {
            const uint8Array = new Uint8Array([72, 101, 108, 108, 111]); // "Hello"
            await formData.appendFile("file", {
                data: uint8Array,
                filename: "binary.bin",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="binary.bin"');
            expect(serialized).toContain("Hello");
        });

        it("serializes other typed arrays", async () => {
            const int16Array = new Int16Array([1000, 2000, 3000]);
            await formData.appendFile("file", {
                data: int16Array,
                filename: "numbers.bin",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="numbers.bin"');
        });

        it("serializes Buffer data with filename", async () => {
            if (typeof Buffer !== "undefined" && typeof Buffer.isBuffer === "function") {
                const buffer = Buffer.from("test content");
                await formData.appendFile("file", {
                    data: buffer,
                    filename: "test.txt",
                });

                const serialized = await serializeFormData(formData.getRequest().body);
                expect(serialized).toContain('filename="test.txt"');
                expect(serialized).toContain("test content");
            }
        });
    });

    describe("Text and primitive types", () => {
        it("serializes string as regular form field", async () => {
            formData.append("text", "test string");

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('name="text"');
            expect(serialized).not.toContain("filename=");
            expect(serialized).toContain("test string");
        });

        it("serializes numbers and booleans as strings", async () => {
            formData.append("number", 12345);
            formData.append("flag", true);

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain("12345");
            expect(serialized).toContain("true");
        });
    });

    describe("Edge cases and error handling", () => {
        it("handles empty filename gracefully", async () => {
            await formData.appendFile("file", {
                data: new Blob(["content"], { type: "text/plain" }),
                filename: "",
            });

            const serialized = await serializeFormData(formData.getRequest().body);
            expect(serialized).toContain('filename="blob"'); // Default fallback
        });

        it("handles multiple files in single form", async () => {
            await formData.appendFile("file1", {
                data: new Blob(["content1"], { type: "text/plain" }),
                filename: "file1.txt",
            });
            await formData.appendFile("file2", {
                data: new Blob(["content2"], { type: "text/plain" }),
                filename: "file2.txt",
            });
            formData.append("text", "regular field");

            const serialized = await serializeFormData(formData.getRequest().body);

            expect(serialized).toContain('filename="file1.txt"');
            expect(serialized).toContain('filename="file2.txt"');
            expect(serialized).toContain('name="text"');
            expect(serialized).not.toContain('filename="text"');
        });
    });

    describe("Request structure", () => {
        it("returns correct request structure", async () => {
            await formData.appendFile("file", {
                data: new Blob(["content"], { type: "text/plain" }),
                filename: "test.txt",
            });

            const request = formData.getRequest();

            expect(request).toHaveProperty("body");
            expect(request).toHaveProperty("headers");
            expect(request).toHaveProperty("duplex");
            expect(request.body).toBeInstanceOf(FormData);
            expect(request.headers).toEqual({});
            expect(request.duplex).toBe("half");
        });

        it("generates proper multipart boundary structure", async () => {
            await formData.appendFile("file", {
                data: new Blob(["test content"], { type: "text/plain" }),
                filename: "test.txt",
            });
            formData.append("field", "value");

            const serialized = await serializeFormData(formData.getRequest().body);

            expect(serialized).toMatch(/------formdata-undici-\w+|------WebKitFormBoundary\w+/);
            expect(serialized).toContain("Content-Disposition: form-data;");
            expect(serialized).toMatch(/------formdata-undici-\w+--|------WebKitFormBoundary\w+--/);
        });
    });

    describe("Factory function", () => {
        it("returns FormDataWrapper instance", async () => {
            const formData = await newFormData();
            expect(formData).toBeInstanceOf(FormDataWrapper);
        });

        it("creates independent instances", async () => {
            const formData1 = await newFormData();
            const formData2 = await newFormData();

            await formData1.setup();
            await formData2.setup();

            formData1.append("test1", "value1");
            formData2.append("test2", "value2");

            const request1 = formData1.getRequest() as { body: FormData };
            const request2 = formData2.getRequest() as { body: FormData };

            const entries1 = Array.from(request1.body.entries());
            const entries2 = Array.from(request2.body.entries());

            expect(entries1).toHaveLength(1);
            expect(entries2).toHaveLength(1);
            expect(entries1[0][0]).toBe("test1");
            expect(entries2[0][0]).toBe("test2");
        });
    });
});
