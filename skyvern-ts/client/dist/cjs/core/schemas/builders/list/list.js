"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.list = list;
const Schema_js_1 = require("../../Schema.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
const maybeSkipValidation_js_1 = require("../../utils/maybeSkipValidation.js");
const index_js_1 = require("../schema-utils/index.js");
function list(schema) {
    const baseSchema = {
        parse: (raw, opts) => validateAndTransformArray(raw, (item, index) => {
            var _a;
            return schema.parse(item, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), `[${index}]`] }));
        }),
        json: (parsed, opts) => validateAndTransformArray(parsed, (item, index) => {
            var _a;
            return schema.json(item, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), `[${index}]`] }));
        }),
        getType: () => Schema_js_1.SchemaType.LIST,
    };
    return Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_1.getSchemaUtils)(baseSchema));
}
function validateAndTransformArray(value, transformItem) {
    if (!Array.isArray(value)) {
        return {
            ok: false,
            errors: [
                {
                    message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, "list"),
                    path: [],
                },
            ],
        };
    }
    const maybeValidItems = value.map((item, index) => transformItem(item, index));
    return maybeValidItems.reduce((acc, item) => {
        if (acc.ok && item.ok) {
            return {
                ok: true,
                value: [...acc.value, item.value],
            };
        }
        const errors = [];
        if (!acc.ok) {
            errors.push(...acc.errors);
        }
        if (!item.ok) {
            errors.push(...item.errors);
        }
        return {
            ok: false,
            errors,
        };
    }, { ok: true, value: [] });
}
