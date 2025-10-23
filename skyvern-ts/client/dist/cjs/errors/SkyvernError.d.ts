import type * as core from "../core/index.js";
export declare class SkyvernError extends Error {
    readonly statusCode?: number;
    readonly body?: unknown;
    readonly rawResponse?: core.RawResponse;
    constructor({ message, statusCode, body, rawResponse, }: {
        message?: string;
        statusCode?: number;
        body?: unknown;
        rawResponse?: core.RawResponse;
    });
}
