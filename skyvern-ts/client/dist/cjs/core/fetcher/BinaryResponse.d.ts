import type { ResponseWithBody } from "./ResponseWithBody.js";
export type BinaryResponse = {
    /** [MDN Reference](https://developer.mozilla.org/docs/Web/API/Request/bodyUsed) */
    bodyUsed: boolean;
    /**
     * Returns a ReadableStream of the response body.
     * [MDN Reference](https://developer.mozilla.org/docs/Web/API/Request/body)
     */
    stream: () => ReadableStream<Uint8Array>;
    /** [MDN Reference](https://developer.mozilla.org/docs/Web/API/Request/arrayBuffer) */
    arrayBuffer: () => Promise<ArrayBuffer>;
    /** [MDN Reference](https://developer.mozilla.org/docs/Web/API/Request/blob) */
    blob: () => Promise<Blob>;
    /**
     * [MDN Reference](https://developer.mozilla.org/docs/Web/API/Request/bytes)
     * Some versions of the Fetch API may not support this method.
     */
    bytes?(): Promise<Uint8Array>;
};
export declare function getBinaryResponse(response: ResponseWithBody): BinaryResponse;
