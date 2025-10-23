"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.lazy = lazy;
exports.constructLazyBaseSchema = constructLazyBaseSchema;
exports.getMemoizedSchema = getMemoizedSchema;
const index_js_1 = require("../schema-utils/index.js");
function lazy(getter) {
    const baseSchema = constructLazyBaseSchema(getter);
    return Object.assign(Object.assign({}, baseSchema), (0, index_js_1.getSchemaUtils)(baseSchema));
}
function constructLazyBaseSchema(getter) {
    return {
        parse: (raw, opts) => getMemoizedSchema(getter).parse(raw, opts),
        json: (parsed, opts) => getMemoizedSchema(getter).json(parsed, opts),
        getType: () => getMemoizedSchema(getter).getType(),
    };
}
function getMemoizedSchema(getter) {
    const castedGetter = getter;
    if (castedGetter.__zurg_memoized == null) {
        castedGetter.__zurg_memoized = getter();
    }
    return castedGetter.__zurg_memoized;
}
