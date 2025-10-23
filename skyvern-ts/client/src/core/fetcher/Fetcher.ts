import { toJson } from "../json.js";
import type { APIResponse } from "./APIResponse.js";
import { createRequestUrl } from "./createRequestUrl.js";
import type { EndpointMetadata } from "./EndpointMetadata.js";
import { EndpointSupplier } from "./EndpointSupplier.js";
import { getErrorResponseBody } from "./getErrorResponseBody.js";
import { getFetchFn } from "./getFetchFn.js";
import { getRequestBody } from "./getRequestBody.js";
import { getResponseBody } from "./getResponseBody.js";
import { makeRequest } from "./makeRequest.js";
import { abortRawResponse, toRawResponse, unknownRawResponse } from "./RawResponse.js";
import { requestWithRetries } from "./requestWithRetries.js";

export type FetchFunction = <R = unknown>(args: Fetcher.Args) => Promise<APIResponse<R, Fetcher.Error>>;

export declare namespace Fetcher {
    export interface Args {
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

    export type Error = FailedStatusCodeError | NonJsonError | TimeoutError | UnknownError;

    export interface FailedStatusCodeError {
        reason: "status-code";
        statusCode: number;
        body: unknown;
    }

    export interface NonJsonError {
        reason: "non-json";
        statusCode: number;
        rawBody: string;
    }

    export interface TimeoutError {
        reason: "timeout";
    }

    export interface UnknownError {
        reason: "unknown";
        errorMessage: string;
    }
}

async function getHeaders(args: Fetcher.Args): Promise<Record<string, string>> {
    const newHeaders: Record<string, string> = {};
    if (args.body !== undefined && args.contentType != null) {
        newHeaders["Content-Type"] = args.contentType;
    }

    if (args.headers == null) {
        return newHeaders;
    }

    for (const [key, value] of Object.entries(args.headers)) {
        const result = await EndpointSupplier.get(value, { endpointMetadata: args.endpointMetadata ?? {} });
        if (typeof result === "string") {
            newHeaders[key] = result;
            continue;
        }
        if (result == null) {
            continue;
        }
        newHeaders[key] = `${result}`;
    }
    return newHeaders;
}

export async function fetcherImpl<R = unknown>(args: Fetcher.Args): Promise<APIResponse<R, Fetcher.Error>> {
    const url = createRequestUrl(args.url, args.queryParameters);
    const requestBody: BodyInit | undefined = await getRequestBody({
        body: args.body,
        type: args.requestType === "json" ? "json" : "other",
    });
    const fetchFn = await getFetchFn();

    try {
        const response = await requestWithRetries(
            async () =>
                makeRequest(
                    fetchFn,
                    url,
                    args.method,
                    await getHeaders(args),
                    requestBody,
                    args.timeoutMs,
                    args.abortSignal,
                    args.withCredentials,
                    args.duplex,
                ),
            args.maxRetries,
        );

        if (response.status >= 200 && response.status < 400) {
            return {
                ok: true,
                body: (await getResponseBody(response, args.responseType)) as R,
                headers: response.headers,
                rawResponse: toRawResponse(response),
            };
        } else {
            return {
                ok: false,
                error: {
                    reason: "status-code",
                    statusCode: response.status,
                    body: await getErrorResponseBody(response),
                },
                rawResponse: toRawResponse(response),
            };
        }
    } catch (error) {
        if (args.abortSignal?.aborted) {
            return {
                ok: false,
                error: {
                    reason: "unknown",
                    errorMessage: "The user aborted a request",
                },
                rawResponse: abortRawResponse,
            };
        } else if (error instanceof Error && error.name === "AbortError") {
            return {
                ok: false,
                error: {
                    reason: "timeout",
                },
                rawResponse: abortRawResponse,
            };
        } else if (error instanceof Error) {
            return {
                ok: false,
                error: {
                    reason: "unknown",
                    errorMessage: error.message,
                },
                rawResponse: unknownRawResponse,
            };
        }

        return {
            ok: false,
            error: {
                reason: "unknown",
                errorMessage: toJson(error),
            },
            rawResponse: unknownRawResponse,
        };
    }
}

export const fetcher: FetchFunction = fetcherImpl;
