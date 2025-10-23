"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.undiscriminatedUnion = undiscriminatedUnion;
const Schema_js_1 = require("../../Schema.js");
const maybeSkipValidation_js_1 = require("../../utils/maybeSkipValidation.js");
const index_js_1 = require("../schema-utils/index.js");
function undiscriminatedUnion(schemas) {
    const baseSchema = {
        parse: (raw, opts) => {
            return validateAndTransformUndiscriminatedUnion((schema, opts) => schema.parse(raw, opts), schemas, opts);
        },
        json: (parsed, opts) => {
            return validateAndTransformUndiscriminatedUnion((schema, opts) => schema.json(parsed, opts), schemas, opts);
        },
        getType: () => Schema_js_1.SchemaType.UNDISCRIMINATED_UNION,
    };
    return Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_1.getSchemaUtils)(baseSchema));
}
function validateAndTransformUndiscriminatedUnion(transform, schemas, opts) {
    const errors = [];
    for (const [index, schema] of schemas.entries()) {
        const transformed = transform(schema, Object.assign(Object.assign({}, opts), { skipValidation: false }));
        if (transformed.ok) {
            return transformed;
        }
        else {
            for (const error of transformed.errors) {
                errors.push({
                    path: error.path,
                    message: `[Variant ${index}] ${error.message}`,
                });
            }
        }
    }
    return {
        ok: false,
        errors,
    };
}
