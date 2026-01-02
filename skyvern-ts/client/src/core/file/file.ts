import type { Uploadable } from "./types.js";

export async function toBinaryUploadRequest(
    file: Uploadable,
): Promise<{ body: Uploadable.FileLike; headers?: Record<string, string> }> {
    const { data, filename, contentLength, contentType } = await getFileWithMetadata(file);
    const request = {
        body: data,
        headers: {} as Record<string, string>,
    };
    if (filename) {
        request.headers["Content-Disposition"] = `attachment; filename="${filename}"`;
    }
    if (contentType) {
        request.headers["Content-Type"] = contentType;
    }
    if (contentLength != null) {
        request.headers["Content-Length"] = contentLength.toString();
    }
    return request;
}

export async function toMultipartDataPart(
    file: Uploadable,
): Promise<{ data: Uploadable.FileLike; filename?: string; contentType?: string }> {
    const { data, filename, contentType } = await getFileWithMetadata(file, {
        noSniffFileSize: true,
    });
    return {
        data,
        filename,
        contentType,
    };
}

async function getFileWithMetadata(
    file: Uploadable,
    { noSniffFileSize }: { noSniffFileSize?: boolean } = {},
): Promise<Uploadable.WithMetadata> {
    if (isFileLike(file)) {
        return getFileWithMetadata(
            {
                data: file,
            },
            { noSniffFileSize },
        );
    }

    if ("path" in file) {
        const fs = await import("fs");
        if (!fs || !fs.createReadStream) {
            throw new Error("File path uploads are not supported in this environment.");
        }
        const data = fs.createReadStream(file.path);
        const contentLength =
            file.contentLength ?? (noSniffFileSize === true ? undefined : await tryGetFileSizeFromPath(file.path));
        const filename = file.filename ?? getNameFromPath(file.path);
        return {
            data,
            filename,
            contentType: file.contentType,
            contentLength,
        };
    }
    if ("data" in file) {
        const data = file.data;
        const contentLength =
            file.contentLength ??
            (await tryGetContentLengthFromFileLike(data, {
                noSniffFileSize,
            }));
        const filename = file.filename ?? tryGetNameFromFileLike(data);
        return {
            data,
            filename,
            contentType: file.contentType ?? tryGetContentTypeFromFileLike(data),
            contentLength,
        };
    }

    throw new Error(`Invalid FileUpload of type ${typeof file}: ${JSON.stringify(file)}`);
}

function isFileLike(value: unknown): value is Uploadable.FileLike {
    return (
        isBuffer(value) ||
        isArrayBufferView(value) ||
        isArrayBuffer(value) ||
        isUint8Array(value) ||
        isBlob(value) ||
        isFile(value) ||
        isStreamLike(value) ||
        isReadableStream(value)
    );
}

async function tryGetFileSizeFromPath(path: string): Promise<number | undefined> {
    try {
        const fs = await import("fs");
        if (!fs || !fs.promises || !fs.promises.stat) {
            return undefined;
        }
        const fileStat = await fs.promises.stat(path);
        return fileStat.size;
    } catch (_fallbackError) {
        return undefined;
    }
}

function tryGetNameFromFileLike(data: Uploadable.FileLike): string | undefined {
    if (isNamedValue(data)) {
        return data.name;
    }
    if (isPathedValue(data)) {
        return getNameFromPath(data.path.toString());
    }
    return undefined;
}

async function tryGetContentLengthFromFileLike(
    data: Uploadable.FileLike,
    { noSniffFileSize }: { noSniffFileSize?: boolean } = {},
): Promise<number | undefined> {
    if (isBuffer(data)) {
        return data.length;
    }
    if (isArrayBufferView(data)) {
        return data.byteLength;
    }
    if (isArrayBuffer(data)) {
        return data.byteLength;
    }
    if (isBlob(data)) {
        return data.size;
    }
    if (isFile(data)) {
        return data.size;
    }
    if (noSniffFileSize === true) {
        return undefined;
    }
    if (isPathedValue(data)) {
        return await tryGetFileSizeFromPath(data.path.toString());
    }
    return undefined;
}

function tryGetContentTypeFromFileLike(data: Uploadable.FileLike): string | undefined {
    if (isBlob(data)) {
        return data.type;
    }
    if (isFile(data)) {
        return data.type;
    }

    return undefined;
}

function getNameFromPath(path: string): string | undefined {
    const lastForwardSlash = path.lastIndexOf("/");
    const lastBackSlash = path.lastIndexOf("\\");
    const lastSlashIndex = Math.max(lastForwardSlash, lastBackSlash);
    return lastSlashIndex >= 0 ? path.substring(lastSlashIndex + 1) : path;
}

type NamedValue = {
    name: string;
} & unknown;

type PathedValue = {
    path: string | { toString(): string };
} & unknown;

type StreamLike = {
    read?: () => unknown;
    pipe?: (dest: unknown) => unknown;
} & unknown;

function isNamedValue(value: unknown): value is NamedValue {
    return typeof value === "object" && value != null && "name" in value;
}

function isPathedValue(value: unknown): value is PathedValue {
    return typeof value === "object" && value != null && "path" in value;
}

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
    return typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView(value);
}

function isArrayBuffer(value: unknown): value is ArrayBuffer {
    return typeof ArrayBuffer !== "undefined" && value instanceof ArrayBuffer;
}

function isUint8Array(value: unknown): value is Uint8Array {
    return typeof Uint8Array !== "undefined" && value instanceof Uint8Array;
}

function isBlob(value: unknown): value is Blob {
    return typeof Blob !== "undefined" && value instanceof Blob;
}

function isFile(value: unknown): value is File {
    return typeof File !== "undefined" && value instanceof File;
}
