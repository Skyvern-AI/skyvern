"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.getBinaryResponse = getBinaryResponse;
function getBinaryResponse(response) {
    const binaryResponse = {
        get bodyUsed() {
            return response.bodyUsed;
        },
        stream: () => response.body,
        arrayBuffer: response.arrayBuffer.bind(response),
        blob: response.blob.bind(response),
    };
    if ("bytes" in response && typeof response.bytes === "function") {
        binaryResponse.bytes = response.bytes.bind(response);
    }
    return binaryResponse;
}
