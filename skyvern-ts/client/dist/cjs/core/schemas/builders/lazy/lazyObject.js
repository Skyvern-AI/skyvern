"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.lazyObject = lazyObject;
const index_js_1 = require("../object/index.js");
const index_js_2 = require("../object-like/index.js");
const index_js_3 = require("../schema-utils/index.js");
const lazy_js_1 = require("./lazy.js");
function lazyObject(getter) {
    const baseSchema = Object.assign(Object.assign({}, (0, lazy_js_1.constructLazyBaseSchema)(getter)), { _getRawProperties: () => (0, lazy_js_1.getMemoizedSchema)(getter)._getRawProperties(), _getParsedProperties: () => (0, lazy_js_1.getMemoizedSchema)(getter)._getParsedProperties() });
    return Object.assign(Object.assign(Object.assign(Object.assign({}, baseSchema), (0, index_js_3.getSchemaUtils)(baseSchema)), (0, index_js_2.getObjectLikeUtils)(baseSchema)), (0, index_js_1.getObjectUtils)(baseSchema));
}
