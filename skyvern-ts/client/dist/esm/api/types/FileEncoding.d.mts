/** Supported file content encodings. */
export declare const FileEncoding: {
    readonly Base64: "base64";
    readonly Utf8: "utf-8";
};
export type FileEncoding = (typeof FileEncoding)[keyof typeof FileEncoding];
