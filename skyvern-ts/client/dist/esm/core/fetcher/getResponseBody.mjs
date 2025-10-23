var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
import { fromJson } from "../json.mjs";
import { getBinaryResponse } from "./BinaryResponse.mjs";
import { isResponseWithBody } from "./ResponseWithBody.mjs";
export function getResponseBody(response, responseType) {
    return __awaiter(this, void 0, void 0, function* () {
        if (!isResponseWithBody(response)) {
            return undefined;
        }
        switch (responseType) {
            case "binary-response":
                return getBinaryResponse(response);
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
                const responseBody = fromJson(text);
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
