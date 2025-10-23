import { SchemaType } from "../../Schema.mjs";
import { entries } from "../../utils/entries.mjs";
import { filterObject } from "../../utils/filterObject.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { isPlainObject } from "../../utils/isPlainObject.mjs";
import { keys } from "../../utils/keys.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { partition } from "../../utils/partition.mjs";
import { getObjectLikeUtils } from "../object-like/index.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
import { isProperty } from "./property.mjs";
export function object(schemas) {
    const baseSchema = {
        _getRawProperties: () => Object.entries(schemas).map(([parsedKey, propertySchema]) => isProperty(propertySchema) ? propertySchema.rawKey : parsedKey),
        _getParsedProperties: () => keys(schemas),
        parse: (raw, opts) => {
            const rawKeyToProperty = {};
            const requiredKeys = [];
            for (const [parsedKey, schemaOrObjectProperty] of entries(schemas)) {
                const rawKey = isProperty(schemaOrObjectProperty) ? schemaOrObjectProperty.rawKey : parsedKey;
                const valueSchema = isProperty(schemaOrObjectProperty)
                    ? schemaOrObjectProperty.valueSchema
                    : schemaOrObjectProperty;
                const property = {
                    rawKey,
                    parsedKey: parsedKey,
                    valueSchema,
                };
                rawKeyToProperty[rawKey] = property;
                if (isSchemaRequired(valueSchema)) {
                    requiredKeys.push(rawKey);
                }
            }
            return validateAndTransformObject({
                value: raw,
                requiredKeys,
                getProperty: (rawKey) => {
                    const property = rawKeyToProperty[rawKey];
                    if (property == null) {
                        return undefined;
                    }
                    return {
                        transformedKey: property.parsedKey,
                        transform: (propertyValue) => {
                            var _a;
                            return property.valueSchema.parse(propertyValue, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), rawKey] }));
                        },
                    };
                },
                unrecognizedObjectKeys: opts === null || opts === void 0 ? void 0 : opts.unrecognizedObjectKeys,
                skipValidation: opts === null || opts === void 0 ? void 0 : opts.skipValidation,
                breadcrumbsPrefix: opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix,
                omitUndefined: opts === null || opts === void 0 ? void 0 : opts.omitUndefined,
            });
        },
        json: (parsed, opts) => {
            const requiredKeys = [];
            for (const [parsedKey, schemaOrObjectProperty] of entries(schemas)) {
                const valueSchema = isProperty(schemaOrObjectProperty)
                    ? schemaOrObjectProperty.valueSchema
                    : schemaOrObjectProperty;
                if (isSchemaRequired(valueSchema)) {
                    requiredKeys.push(parsedKey);
                }
            }
            return validateAndTransformObject({
                value: parsed,
                requiredKeys,
                getProperty: (parsedKey) => {
                    const property = schemas[parsedKey];
                    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
                    if (property == null) {
                        return undefined;
                    }
                    if (isProperty(property)) {
                        return {
                            transformedKey: property.rawKey,
                            transform: (propertyValue) => {
                                var _a;
                                return property.valueSchema.json(propertyValue, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), parsedKey] }));
                            },
                        };
                    }
                    else {
                        return {
                            transformedKey: parsedKey,
                            transform: (propertyValue) => {
                                var _a;
                                return property.json(propertyValue, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), parsedKey] }));
                            },
                        };
                    }
                },
                unrecognizedObjectKeys: opts === null || opts === void 0 ? void 0 : opts.unrecognizedObjectKeys,
                skipValidation: opts === null || opts === void 0 ? void 0 : opts.skipValidation,
                breadcrumbsPrefix: opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix,
                omitUndefined: opts === null || opts === void 0 ? void 0 : opts.omitUndefined,
            });
        },
        getType: () => SchemaType.OBJECT,
    };
    return Object.assign(Object.assign(Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema)), getObjectLikeUtils(baseSchema)), getObjectUtils(baseSchema));
}
function validateAndTransformObject({ value, requiredKeys, getProperty, unrecognizedObjectKeys = "fail", skipValidation = false, breadcrumbsPrefix = [], }) {
    if (!isPlainObject(value)) {
        return {
            ok: false,
            errors: [
                {
                    path: breadcrumbsPrefix,
                    message: getErrorMessageForIncorrectType(value, "object"),
                },
            ],
        };
    }
    const missingRequiredKeys = new Set(requiredKeys);
    const errors = [];
    const transformed = {};
    for (const [preTransformedKey, preTransformedItemValue] of Object.entries(value)) {
        const property = getProperty(preTransformedKey);
        if (property != null) {
            missingRequiredKeys.delete(preTransformedKey);
            const value = property.transform(preTransformedItemValue);
            if (value.ok) {
                transformed[property.transformedKey] = value.value;
            }
            else {
                transformed[preTransformedKey] = preTransformedItemValue;
                errors.push(...value.errors);
            }
        }
        else {
            switch (unrecognizedObjectKeys) {
                case "fail":
                    errors.push({
                        path: [...breadcrumbsPrefix, preTransformedKey],
                        message: `Unexpected key "${preTransformedKey}"`,
                    });
                    break;
                case "strip":
                    break;
                case "passthrough":
                    transformed[preTransformedKey] = preTransformedItemValue;
                    break;
            }
        }
    }
    errors.push(...requiredKeys
        .filter((key) => missingRequiredKeys.has(key))
        .map((key) => ({
        path: breadcrumbsPrefix,
        message: `Missing required key "${key}"`,
    })));
    if (errors.length === 0 || skipValidation) {
        return {
            ok: true,
            value: transformed,
        };
    }
    else {
        return {
            ok: false,
            errors,
        };
    }
}
export function getObjectUtils(schema) {
    return {
        extend: (extension) => {
            const baseSchema = {
                _getParsedProperties: () => [...schema._getParsedProperties(), ...extension._getParsedProperties()],
                _getRawProperties: () => [...schema._getRawProperties(), ...extension._getRawProperties()],
                parse: (raw, opts) => {
                    return validateAndTransformExtendedObject({
                        extensionKeys: extension._getRawProperties(),
                        value: raw,
                        transformBase: (rawBase) => schema.parse(rawBase, opts),
                        transformExtension: (rawExtension) => extension.parse(rawExtension, opts),
                    });
                },
                json: (parsed, opts) => {
                    return validateAndTransformExtendedObject({
                        extensionKeys: extension._getParsedProperties(),
                        value: parsed,
                        transformBase: (parsedBase) => schema.json(parsedBase, opts),
                        transformExtension: (parsedExtension) => extension.json(parsedExtension, opts),
                    });
                },
                getType: () => SchemaType.OBJECT,
            };
            return Object.assign(Object.assign(Object.assign(Object.assign({}, baseSchema), getSchemaUtils(baseSchema)), getObjectLikeUtils(baseSchema)), getObjectUtils(baseSchema));
        },
        passthrough: () => {
            const baseSchema = {
                _getParsedProperties: () => schema._getParsedProperties(),
                _getRawProperties: () => schema._getRawProperties(),
                parse: (raw, opts) => {
                    const transformed = schema.parse(raw, Object.assign(Object.assign({}, opts), { unrecognizedObjectKeys: "passthrough" }));
                    if (!transformed.ok) {
                        return transformed;
                    }
                    return {
                        ok: true,
                        value: Object.assign(Object.assign({}, raw), transformed.value),
                    };
                },
                json: (parsed, opts) => {
                    const transformed = schema.json(parsed, Object.assign(Object.assign({}, opts), { unrecognizedObjectKeys: "passthrough" }));
                    if (!transformed.ok) {
                        return transformed;
                    }
                    return {
                        ok: true,
                        value: Object.assign(Object.assign({}, parsed), transformed.value),
                    };
                },
                getType: () => SchemaType.OBJECT,
            };
            return Object.assign(Object.assign(Object.assign(Object.assign({}, baseSchema), getSchemaUtils(baseSchema)), getObjectLikeUtils(baseSchema)), getObjectUtils(baseSchema));
        },
    };
}
function validateAndTransformExtendedObject({ extensionKeys, value, transformBase, transformExtension, }) {
    const extensionPropertiesSet = new Set(extensionKeys);
    const [extensionProperties, baseProperties] = partition(keys(value), (key) => extensionPropertiesSet.has(key));
    const transformedBase = transformBase(filterObject(value, baseProperties));
    const transformedExtension = transformExtension(filterObject(value, extensionProperties));
    if (transformedBase.ok && transformedExtension.ok) {
        return {
            ok: true,
            value: Object.assign(Object.assign({}, transformedBase.value), transformedExtension.value),
        };
    }
    else {
        return {
            ok: false,
            errors: [
                ...(transformedBase.ok ? [] : transformedBase.errors),
                ...(transformedExtension.ok ? [] : transformedExtension.errors),
            ],
        };
    }
}
function isSchemaRequired(schema) {
    return !isSchemaOptional(schema);
}
function isSchemaOptional(schema) {
    switch (schema.getType()) {
        case SchemaType.ANY:
        case SchemaType.UNKNOWN:
        case SchemaType.OPTIONAL:
        case SchemaType.OPTIONAL_NULLABLE:
            return true;
        default:
            return false;
    }
}
