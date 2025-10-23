"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.createRequestUrl = createRequestUrl;
const qs_js_1 = require("../url/qs.js");
function createRequestUrl(baseUrl, queryParameters) {
    const queryString = (0, qs_js_1.toQueryString)(queryParameters, { arrayFormat: "repeat" });
    return queryString ? `${baseUrl}?${queryString}` : baseUrl;
}
