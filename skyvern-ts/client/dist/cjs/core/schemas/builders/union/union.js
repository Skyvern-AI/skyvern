"use strict";
var __rest = (this && this.__rest) || function (s, e) {
    var t = {};
    for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p) && e.indexOf(p) < 0)
        t[p] = s[p];
    if (s != null && typeof Object.getOwnPropertySymbols === "function")
        for (var i = 0, p = Object.getOwnPropertySymbols(s); i < p.length; i++) {
            if (e.indexOf(p[i]) < 0 && Object.prototype.propertyIsEnumerable.call(s, p[i]))
                t[p[i]] = s[p[i]];
        }
    return t;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.union = union;
const Schema_js_1 = require("../../Schema.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
const isPlainObject_js_1 = require("../../utils/isPlainObject.js");
const keys_js_1 = require("../../utils/keys.js");
const maybeSkipValidation_js_1 = require("../../utils/maybeSkipValidation.js");
const index_js_1 = require("../enum/index.js");
const index_js_2 = require("../object-like/index.js");
const index_js_3 = require("../schema-utils/index.js");
function union(discriminant, union) {
    const rawDiscriminant = typeof discriminant === "string" ? discriminant : discriminant.rawDiscriminant;
    const parsedDiscriminant = typeof discriminant === "string"
        ? discriminant
        : discriminant.parsedDiscriminant;
    const discriminantValueSchema = (0, index_js_1.enum_)((0, keys_js_1.keys)(union));
    const baseSchema = {
        parse: (raw, opts) => {
            return transformAndValidateUnion({
                value: raw,
                discriminant: rawDiscriminant,
                transformedDiscriminant: parsedDiscriminant,
                transformDiscriminantValue: (discriminantValue) => {
                    var _a;
                    return discriminantValueSchema.parse(discriminantValue, {
                        allowUnrecognizedEnumValues: opts === null || opts === void 0 ? void 0 : opts.allowUnrecognizedUnionMembers,
                        breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), rawDiscriminant],
                    });
                },
                getAdditionalPropertiesSchema: (discriminantValue) => union[discriminantValue],
                allowUnrecognizedUnionMembers: opts === null || opts === void 0 ? void 0 : opts.allowUnrecognizedUnionMembers,
                transformAdditionalProperties: (additionalProperties, additionalPropertiesSchema) => additionalPropertiesSchema.parse(additionalProperties, opts),
                breadcrumbsPrefix: opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix,
            });
        },
        json: (parsed, opts) => {
            return transformAndValidateUnion({
                value: parsed,
                discriminant: parsedDiscriminant,
                transformedDiscriminant: rawDiscriminant,
                transformDiscriminantValue: (discriminantValue) => {
                    var _a;
                    return discriminantValueSchema.json(discriminantValue, {
                        allowUnrecognizedEnumValues: opts === null || opts === void 0 ? void 0 : opts.allowUnrecognizedUnionMembers,
                        breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), parsedDiscriminant],
                    });
                },
                getAdditionalPropertiesSchema: (discriminantValue) => union[discriminantValue],
                allowUnrecognizedUnionMembers: opts === null || opts === void 0 ? void 0 : opts.allowUnrecognizedUnionMembers,
                transformAdditionalProperties: (additionalProperties, additionalPropertiesSchema) => additionalPropertiesSchema.json(additionalProperties, opts),
                breadcrumbsPrefix: opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix,
            });
        },
        getType: () => Schema_js_1.SchemaType.UNION,
    };
    return Object.assign(Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_3.getSchemaUtils)(baseSchema)), (0, index_js_2.getObjectLikeUtils)(baseSchema));
}
function transformAndValidateUnion({ value, discriminant, transformedDiscriminant, transformDiscriminantValue, getAdditionalPropertiesSchema, allowUnrecognizedUnionMembers = false, transformAdditionalProperties, breadcrumbsPrefix = [], }) {
    if (!(0, isPlainObject_js_1.isPlainObject)(value)) {
        return {
            ok: false,
            errors: [
                {
                    path: breadcrumbsPrefix,
                    message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, "object"),
                },
            ],
        };
    }
    const _a = value, _b = discriminant, discriminantValue = _a[_b], additionalProperties = __rest(_a, [typeof _b === "symbol" ? _b : _b + ""]);
    if (discriminantValue == null) {
        return {
            ok: false,
            errors: [
                {
                    path: breadcrumbsPrefix,
                    message: `Missing discriminant ("${discriminant}")`,
                },
            ],
        };
    }
    const transformedDiscriminantValue = transformDiscriminantValue(discriminantValue);
    if (!transformedDiscriminantValue.ok) {
        return {
            ok: false,
            errors: transformedDiscriminantValue.errors,
        };
    }
    const additionalPropertiesSchema = getAdditionalPropertiesSchema(transformedDiscriminantValue.value);
    if (additionalPropertiesSchema == null) {
        if (allowUnrecognizedUnionMembers) {
            return {
                ok: true,
                value: Object.assign({ [transformedDiscriminant]: transformedDiscriminantValue.value }, additionalProperties),
            };
        }
        else {
            return {
                ok: false,
                errors: [
                    {
                        path: [...breadcrumbsPrefix, discriminant],
                        message: "Unexpected discriminant value",
                    },
                ],
            };
        }
    }
    const transformedAdditionalProperties = transformAdditionalProperties(additionalProperties, additionalPropertiesSchema);
    if (!transformedAdditionalProperties.ok) {
        return transformedAdditionalProperties;
    }
    return {
        ok: true,
        value: Object.assign({ [transformedDiscriminant]: discriminantValue }, transformedAdditionalProperties.value),
    };
}
