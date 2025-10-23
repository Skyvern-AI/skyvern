"use strict";
var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.getResponseBody = getResponseBody;
const json_js_1 = require("../json.js");
const BinaryResponse_js_1 = require("./BinaryResponse.js");
const ResponseWithBody_js_1 = require("./ResponseWithBody.js");
function getResponseBody(response, responseType) {
    return __awaiter(this, void 0, void 0, function* () {
        if (!(0, ResponseWithBody_js_1.isResponseWithBody)(response)) {
            return undefined;
        }
        switch (responseType) {
            case "binary-response":
                return (0, BinaryResponse_js_1.getBinaryResponse)(response);
            case "blob":
                return yield response.blob();
            case "arrayBuffer":
                return yield response.arrayBuffer();
            case "sse":
                return response.body;
            case "streaming":
                return response.body;
            case "text":
                return yield response.text();
        }
        // if responseType is "json" or not specified, try to parse as JSON
        const text = yield response.text();
        if (text.length > 0) {
            try {
                const responseBody = (0, json_js_1.fromJson)(text);
                return responseBody;
            }
            catch (_err) {
                return {
                    ok: false,
                    error: {
                        reason: "non-json",
                        statusCode: response.status,
                        rawBody: text,
                    },
                };
            }
        }
        return undefined;
    });
}
