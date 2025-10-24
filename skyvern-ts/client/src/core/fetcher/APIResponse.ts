import type { RawResponse } from "./RawResponse.js";

/**
 * The response of an API call.
 * It is a successful response or a failed response.
 */
export type APIResponse<Success, Failure> = SuccessfulResponse<Success> | FailedResponse<Failure>;

export interface SuccessfulResponse<T> {
    ok: true;
    body: T;
    /**
     * @deprecated Use `rawResponse` instead
     */
    headers?: Record<string, any>;
    rawResponse: RawResponse;
}

export interface FailedResponse<T> {
    ok: false;
    error: T;
    rawResponse: RawResponse;
}
