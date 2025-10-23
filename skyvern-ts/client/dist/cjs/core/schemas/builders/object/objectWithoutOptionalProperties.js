"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.objectWithoutOptionalProperties = objectWithoutOptionalProperties;
const object_js_1 = require("./object.js");
function objectWithoutOptionalProperties(schemas) {
    return (0, object_js_1.object)(schemas);
}
