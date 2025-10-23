import { Headers } from "./Headers.mjs";
/**
 * A raw response indicating that the request was aborted.
 */
export const abortRawResponse = {
    headers: new Headers(),
    redirected: false,
    status: 499,
    statusText: "Client Closed Request",
    type: "error",
    url: "",
};
/**
 * A raw response indicating an unknown error.
 */
export const unknownRawResponse = {
    headers: new Headers(),
    redirected: false,
    status: 0,
    statusText: "Unknown Error",
    type: "error",
    url: "",
};
/**
 * Converts a `RawResponse` object into a `RawResponse` by extracting its properties,
 * excluding the `body` and `bodyUsed` fields.
 *
 * @param response - The `RawResponse` object to convert.
 * @returns A `RawResponse` object containing the extracted properties of the input response.
 */
export function toRawResponse(response) {
    return {
        headers: response.headers,
        redirected: response.redirected,
        status: response.status,
        statusText: response.statusText,
        type: response.type,
        url: response.url,
    };
}
