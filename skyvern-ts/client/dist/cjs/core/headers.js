"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.mergeHeaders = mergeHeaders;
exports.mergeOnlyDefinedHeaders = mergeOnlyDefinedHeaders;
function mergeHeaders(...headersArray) {
    const result = {};
    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        }
        else if (key in result) {
            delete result[key];
        }
    }
    return result;
}
function mergeOnlyDefinedHeaders(...headersArray) {
    const result = {};
    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        }
    }
    return result;
}
