"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.encodePathParam = encodePathParam;
function encodePathParam(param) {
    if (param === null) {
        return "null";
    }
    const typeofParam = typeof param;
    switch (typeofParam) {
        case "undefined":
            return "undefined";
        case "string":
        case "number":
        case "boolean":
            break;
        default:
            param = String(param);
            break;
    }
    return encodeURIComponent(param);
}
