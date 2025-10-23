/**
 * The raw response from the fetch call excluding the body.
 */
export type RawResponse = Omit<{
    [K in keyof Response as Response[K] extends Function ? never : K]: Response[K];
}, "ok" | "body" | "bodyUsed">;
/**
 * A raw response indicating that the request was aborted.
 */
export declare const abortRawResponse: RawResponse;
/**
 * A raw response indicating an unknown error.
 */
export declare const unknownRawResponse: RawResponse;
/**
 * Converts a `RawResponse` object into a `RawResponse` by extracting its properties,
 * excluding the `body` and `bodyUsed` fields.
 *
 * @param response - The `RawResponse` object to convert.
 * @returns A `RawResponse` object containing the extracted properties of the input response.
 */
export declare function toRawResponse(response: Response): RawResponse;
/**
 * Creates a `RawResponse` from a standard `Response` object.
 */
export interface WithRawResponse<T> {
    readonly data: T;
    readonly rawResponse: RawResponse;
}
