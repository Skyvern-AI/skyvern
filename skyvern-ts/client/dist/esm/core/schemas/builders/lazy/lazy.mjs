import { getSchemaUtils } from "../schema-utils/index.mjs";
export function lazy(getter) {
    const baseSchema = constructLazyBaseSchema(getter);
    return Object.assign(Object.assign({}, baseSchema), getSchemaUtils(baseSchema));
}
export function constructLazyBaseSchema(getter) {
    return {
        parse: (raw, opts) => getMemoizedSchema(getter).parse(raw, opts),
        json: (parsed, opts) => getMemoizedSchema(getter).json(parsed, opts),
        getType: () => getMemoizedSchema(getter).getType(),
    };
}
export function getMemoizedSchema(getter) {
    const castedGetter = getter;
    if (castedGetter.__zurg_memoized == null) {
        castedGetter.__zurg_memoized = getter();
    }
    return castedGetter.__zurg_memoized;
}
