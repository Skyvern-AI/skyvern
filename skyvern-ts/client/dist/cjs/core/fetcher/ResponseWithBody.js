"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isResponseWithBody = isResponseWithBody;
function isResponseWithBody(response) {
    return response.body != null;
}
