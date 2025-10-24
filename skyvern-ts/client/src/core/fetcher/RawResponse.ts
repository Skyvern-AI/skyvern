import { Headers } from "./Headers.js";

/**
 * The raw response from the fetch call excluding the body.
 */
export type RawResponse = Omit<
    {
        [K in keyof Response as Response[K] extends Function ? never : K]: Response[K]; // strips out functions
    },
    "ok" | "body" | "bodyUsed"
>; // strips out body and bodyUsed

/**
 * A raw response indicating that the request was aborted.
 */
export const abortRawResponse: RawResponse = {
    headers: new Headers(),
    redirected: false,
    status: 499,
    statusText: "Client Closed Request",
    type: "error",
    url: "",
} as const;

/**
 * A raw response indicating an unknown error.
 */
export const unknownRawResponse: RawResponse = {
    headers: new Headers(),
    redirected: false,
    status: 0,
    statusText: "Unknown Error",
    type: "error",
    url: "",
} as const;

/**
 * Converts a `RawResponse` object into a `RawResponse` by extracting its properties,
 * excluding the `body` and `bodyUsed` fields.
 *
 * @param response - The `RawResponse` object to convert.
 * @returns A `RawResponse` object containing the extracted properties of the input response.
 */
export function toRawResponse(response: Response): RawResponse {
    return {
        headers: response.headers,
        redirected: response.redirected,
        status: response.status,
        statusText: response.statusText,
        type: response.type,
        url: response.url,
    };
}

/**
 * Creates a `RawResponse` from a standard `Response` object.
 */
export interface WithRawResponse<T> {
    readonly data: T;
    readonly rawResponse: RawResponse;
}
