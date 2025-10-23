import { SchemaType } from "../../Schema.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
export function list(schema) {
    const baseSchema = {
        parse: (raw, opts) => validateAndTransformArray(raw, (item, index) => {
            var _a;
            return schema.parse(item, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), `[${index}]`] }));
        }),
        json: (parsed, opts) => validateAndTransformArray(parsed, (item, index) => {
            var _a;
            return schema.json(item, Object.assign(Object.assign({}, opts), { breadcrumbsPrefix: [...((_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : []), `[${index}]`] }));
        }),
        getType: () => SchemaType.LIST,
    };
    return Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema));
}
function validateAndTransformArray(value, transformItem) {
    if (!Array.isArray(value)) {
        return {
            ok: false,
            errors: [
                {
                    message: getErrorMessageForIncorrectType(value, "list"),
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
