var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
import { toJson } from "../json.mjs";
import { createRequestUrl } from "./createRequestUrl.mjs";
import { EndpointSupplier } from "./EndpointSupplier.mjs";
import { getErrorResponseBody } from "./getErrorResponseBody.mjs";
import { getFetchFn } from "./getFetchFn.mjs";
import { getRequestBody } from "./getRequestBody.mjs";
import { getResponseBody } from "./getResponseBody.mjs";
import { makeRequest } from "./makeRequest.mjs";
import { abortRawResponse, toRawResponse, unknownRawResponse } from "./RawResponse.mjs";
import { requestWithRetries } from "./requestWithRetries.mjs";
function getHeaders(args) {
    return __awaiter(this, void 0, void 0, function* () {
        var _a;
        const newHeaders = {};
        if (args.body !== undefined && args.contentType != null) {
            newHeaders["Content-Type"] = args.contentType;
        }
        if (args.headers == null) {
            return newHeaders;
        }
        for (const [key, value] of Object.entries(args.headers)) {
            const result = yield EndpointSupplier.get(value, { endpointMetadata: (_a = args.endpointMetadata) !== null && _a !== void 0 ? _a : {} });
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
    });
}
export function fetcherImpl(args) {
    return __awaiter(this, void 0, void 0, function* () {
        var _a;
        const url = createRequestUrl(args.url, args.queryParameters);
        const requestBody = yield getRequestBody({
            body: args.body,
            type: args.requestType === "json" ? "json" : "other",
        });
        const fetchFn = yield getFetchFn();
        try {
            const response = yield requestWithRetries(() => __awaiter(this, void 0, void 0, function* () {
                return makeRequest(fetchFn, url, args.method, yield getHeaders(args), requestBody, args.timeoutMs, args.abortSignal, args.withCredentials, args.duplex);
            }), args.maxRetries);
            if (response.status >= 200 && response.status < 400) {
                return {
                    ok: true,
                    body: (yield getResponseBody(response, args.responseType)),
                    headers: response.headers,
                    rawResponse: toRawResponse(response),
                };
            }
            else {
                return {
                    ok: false,
                    error: {
                        reason: "status-code",
                        statusCode: response.status,
                        body: yield getErrorResponseBody(response),
                    },
                    rawResponse: toRawResponse(response),
                };
            }
        }
        catch (error) {
            if ((_a = args.abortSignal) === null || _a === void 0 ? void 0 : _a.aborted) {
                return {
                    ok: false,
                    error: {
                        reason: "unknown",
                        errorMessage: "The user aborted a request",
                    },
                    rawResponse: abortRawResponse,
                };
            }
            else if (error instanceof Error && error.name === "AbortError") {
                return {
                    ok: false,
                    error: {
                        reason: "timeout",
                    },
                    rawResponse: abortRawResponse,
                };
            }
            else if (error instanceof Error) {
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
    });
}
export const fetcher = fetcherImpl;
