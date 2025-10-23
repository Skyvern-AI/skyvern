"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.property = property;
exports.isProperty = isProperty;
function property(rawKey, valueSchema) {
    return {
        rawKey,
        valueSchema,
        isProperty: true,
    };
}
function isProperty(maybeProperty) {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    return maybeProperty.isProperty;
}
