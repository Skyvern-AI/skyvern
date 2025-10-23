import type { APIResponse } from "./APIResponse.mjs";
import type { EndpointMetadata } from "./EndpointMetadata.mjs";
import { EndpointSupplier } from "./EndpointSupplier.mjs";
export type FetchFunction = <R = unknown>(args: Fetcher.Args) => Promise<APIResponse<R, Fetcher.Error>>;
export declare namespace Fetcher {
    interface Args {
        url: string;
        method: string;
        contentType?: string;
        headers?: Record<string, string | EndpointSupplier<string | null | undefined> | null | undefined>;
        queryParameters?: Record<string, unknown>;
        body?: unknown;
        timeoutMs?: number;
        maxRetries?: number;
        withCredentials?: boolean;
        abortSignal?: AbortSignal;
        requestType?: "json" | "file" | "bytes";
        responseType?: "json" | "blob" | "sse" | "streaming" | "text" | "arrayBuffer" | "binary-response";
        duplex?: "half";
        endpointMetadata?: EndpointMetadata;
    }
    type Error = FailedStatusCodeError | NonJsonError | TimeoutError | UnknownError;
    interface FailedStatusCodeError {
        reason: "status-code";
        statusCode: number;
        body: unknown;
    }
    interface NonJsonError {
        reason: "non-json";
        statusCode: number;
        rawBody: string;
    }
    interface TimeoutError {
        reason: "timeout";
    }
    interface UnknownError {
        reason: "unknown";
        errorMessage: string;
    }
}
export declare function fetcherImpl<R = unknown>(args: Fetcher.Args): Promise<APIResponse<R, Fetcher.Error>>;
export declare const fetcher: FetchFunction;
