import { filterObject } from "../../utils/filterObject.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { isPlainObject } from "../../utils/isPlainObject.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
export function getObjectLikeUtils(schema) {
    return {
        withParsedProperties: (properties) => withParsedProperties(schema, properties),
    };
}
/**
 * object-like utils are defined in one file to resolve issues with circular imports
 */
export function withParsedProperties(objectLike, properties) {
    const objectSchema = {
        parse: (raw, opts) => {
            const parsedObject = objectLike.parse(raw, opts);
            if (!parsedObject.ok) {
                return parsedObject;
            }
            const additionalProperties = Object.entries(properties).reduce((processed, [key, value]) => {
                return Object.assign(Object.assign({}, processed), { [key]: typeof value === "function" ? value(parsedObject.value) : value });
            }, {});
            return {
                ok: true,
                value: Object.assign(Object.assign({}, parsedObject.value), additionalProperties),
            };
        },
        json: (parsed, opts) => {
            var _a;
            if (!isPlainObject(parsed)) {
                return {
                    ok: false,
                    errors: [
                        {
                            path: (_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : [],
                            message: getErrorMessageForIncorrectType(parsed, "object"),
                        },
                    ],
                };
            }
            // strip out added properties
            const addedPropertyKeys = new Set(Object.keys(properties));
            const parsedWithoutAddedProperties = filterObject(parsed, Object.keys(parsed).filter((key) => !addedPropertyKeys.has(key)));
            return objectLike.json(parsedWithoutAddedProperties, opts);
        },
        getType: () => objectLike.getType(),
    };
    return Object.assign(Object.assign(Object.assign({}, objectSchema), getSchemaUtils(objectSchema)), getObjectLikeUtils(objectSchema));
}
