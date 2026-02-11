import { toMultipartDataPart, type Uploadable } from "../../core/file/index.js";
import { toJson } from "../../core/json.js";
import { RUNTIME } from "../runtime/index.js";

interface FormDataRequest<Body> {
    body: Body;
    headers: Record<string, string>;
    duplex?: "half";
}

export async function newFormData(): Promise<FormDataWrapper> {
    return new FormDataWrapper();
}

export class FormDataWrapper {
    private fd: FormData = new FormData();

    public async setup(): Promise<void> {
        // noop
    }

    public append(key: string, value: unknown): void {
        this.fd.append(key, String(value));
    }

    public async appendFile(key: string, value: Uploadable): Promise<void> {
        const { data, filename, contentType } = await toMultipartDataPart(value);
        const blob = await convertToBlob(data, contentType);
        if (filename) {
            this.fd.append(key, blob, filename);
        } else {
            this.fd.append(key, blob);
        }
    }

    public getRequest(): FormDataRequest<FormData> {
        return {
            body: this.fd,
            headers: {},
            duplex: "half" as const,
        };
    }
}

type StreamLike = {
    read?: () => unknown;
    pipe?: (dest: unknown) => unknown;
} & unknown;

function isStreamLike(value: unknown): value is StreamLike {
    return typeof value === "object" && value != null && ("read" in value || "pipe" in value);
}

function isReadableStream(value: unknown): value is ReadableStream {
    return typeof value === "object" && value != null && "getReader" in value;
}

function isBuffer(value: unknown): value is Buffer {
    return typeof Buffer !== "undefined" && Buffer.isBuffer && Buffer.isBuffer(value);
}

function isArrayBufferView(value: unknown): value is ArrayBufferView {
    return ArrayBuffer.isView(value);
}

async function streamToBuffer(stream: unknown): Promise<Buffer> {
    if (RUNTIME.type === "node") {
        const { Readable } = await import("stream");

        if (stream instanceof Readable) {
            const chunks: Buffer[] = [];
            for await (const chunk of stream) {
                chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
            }
            return Buffer.concat(chunks);
        }
    }

    if (isReadableStream(stream)) {
        const reader = stream.getReader();
        const chunks: Uint8Array[] = [];

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value);
            }
        } finally {
            reader.releaseLock();
        }

        const totalLength = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
        const result = new Uint8Array(totalLength);
        let offset = 0;
        for (const chunk of chunks) {
            result.set(chunk, offset);
            offset += chunk.length;
        }

        return Buffer.from(result);
    }

    throw new Error(
        `Unsupported stream type: ${typeof stream}. Expected Node.js Readable stream or Web ReadableStream.`,
    );
}

async function convertToBlob(value: unknown, contentType?: string): Promise<Blob> {
    if (isStreamLike(value) || isReadableStream(value)) {
        const buffer = await streamToBuffer(value);
        return new Blob([buffer], { type: contentType });
    }

    if (value instanceof Blob) {
        return value;
    }

    if (isBuffer(value)) {
        return new Blob([value], { type: contentType });
    }

    if (value instanceof ArrayBuffer) {
        return new Blob([value], { type: contentType });
    }

    if (isArrayBufferView(value)) {
        return new Blob([value], { type: contentType });
    }

    if (typeof value === "string") {
        return new Blob([value], { type: contentType });
    }

    if (typeof value === "object" && value !== null) {
        return new Blob([toJson(value)], { type: contentType ?? "application/json" });
    }

    return new Blob([String(value)], { type: contentType });
}
