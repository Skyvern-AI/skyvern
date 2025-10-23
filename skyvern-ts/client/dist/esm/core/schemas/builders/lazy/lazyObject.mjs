import { getObjectUtils } from "../object/index.mjs";
import { getObjectLikeUtils } from "../object-like/index.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
import { constructLazyBaseSchema, getMemoizedSchema } from "./lazy.mjs";
export function lazyObject(getter) {
    const baseSchema = Object.assign(Object.assign({}, constructLazyBaseSchema(getter)), { _getRawProperties: () => getMemoizedSchema(getter)._getRawProperties(), _getParsedProperties: () => getMemoizedSchema(getter)._getParsedProperties() });
    return Object.assign(Object.assign(Object.assign(Object.assign({}, baseSchema), getSchemaUtils(baseSchema)), getObjectLikeUtils(baseSchema)), getObjectUtils(baseSchema));
}
