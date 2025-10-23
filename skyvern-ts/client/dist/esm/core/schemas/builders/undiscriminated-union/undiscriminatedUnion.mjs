import { SchemaType, } from "../../Schema.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
export function undiscriminatedUnion(schemas) {
    const baseSchema = {
        parse: (raw, opts) => {
            return validateAndTransformUndiscriminatedUnion((schema, opts) => schema.parse(raw, opts), schemas, opts);
        },
        json: (parsed, opts) => {
            return validateAndTransformUndiscriminatedUnion((schema, opts) => schema.json(parsed, opts), schemas, opts);
        },
        getType: () => SchemaType.UNDISCRIMINATED_UNION,
    };
    return Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema));
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
