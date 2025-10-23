"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.stringifyValidationError = stringifyValidationError;
function stringifyValidationError(error) {
    if (error.path.length === 0) {
        return error.message;
    }
    return `${error.path.join(" -> ")}: ${error.message}`;
}
