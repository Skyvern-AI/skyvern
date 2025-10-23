export function property(rawKey, valueSchema) {
    return {
        rawKey,
        valueSchema,
        isProperty: true,
    };
}
export function isProperty(maybeProperty) {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    return maybeProperty.isProperty;
}
