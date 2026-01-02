/**
 * A file that can be uploaded. Can be a file-like object (stream, buffer, blob, etc.),
 * a path to a file, or an object with a file-like object and metadata.
 */
export type Uploadable = Uploadable.FileLike | Uploadable.FromPath | Uploadable.WithMetadata;

export namespace Uploadable {
    /**
     * Various file-like objects that can be used to upload a file.
     */
    export type FileLike =
        | ArrayBuffer
        | ArrayBufferLike
        | ArrayBufferView
        | Uint8Array
        | import("buffer").Buffer
        | import("buffer").Blob
        | import("buffer").File
        | import("stream").Readable
        | import("stream/web").ReadableStream
        | globalThis.Blob
        | globalThis.File
        | ReadableStream;

    /**
     * A file path with optional metadata, used for uploading a file from the file system.
     */
    export type FromPath = {
        /** The path to the file to upload */
        path: string;
        /**
         * Optional override for the file name (defaults to basename of path).
         * This is used to set the `Content-Disposition` header in upload requests.
         */
        filename?: string;
        /**
         * Optional MIME type of the file (e.g., 'image/jpeg', 'text/plain').
         * This is used to set the `Content-Type` header in upload requests.
         */
        contentType?: string;
        /**
         * Optional file size in bytes.
         * If not provided, the file size will be determined from the file system.
         * The content length is used to set the `Content-Length` header in upload requests.
         */
        contentLength?: number;
    };

    /**
     * A file-like object with metadata, used for uploading files.
     */
    export type WithMetadata = {
        /** The file data */
        data: FileLike;
        /**
         * Optional override for the file name (defaults to basename of path).
         * This is used to set the `Content-Disposition` header in upload requests.
         */
        filename?: string;
        /**
         * Optional MIME type of the file (e.g., 'image/jpeg', 'text/plain').
         * This is used to set the `Content-Type` header in upload requests.
         *
         * If not provided, the content type may be determined from the data itself.
         * * If the data is a `File`, `Blob`, or similar, the content type will be determined from the file itself, if the type is set.
         * * Any other data type will not have a content type set, and the upload request will use `Content-Type: application/octet-stream` instead.
         */
        contentType?: string;
        /**
         * Optional file size in bytes.
         * The content length is used to set the `Content-Length` header in upload requests.
         * If the content length is not provided and cannot be determined, the upload request will not include the `Content-Length` header, but will use `Transfer-Encoding: chunked` instead.
         *
         * If not provided, the file size will be determined depending on the data type.
         * * If the data is of type `fs.ReadStream` (`createReadStream`), the size will be determined from the file system.
         * * If the data is a `Buffer`, `ArrayBuffer`, `Uint8Array`, `Blob`, `File`, or similar, the size will be determined from the data itself.
         * * If the data is a `Readable` or `ReadableStream`, the size will not be determined.
         */
        contentLength?: number;
    };
}
