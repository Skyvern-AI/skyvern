export function encodePathParam(param: unknown): string {
    if (param === null) {
        return "null";
    }
    const typeofParam = typeof param;
    switch (typeofParam) {
        case "undefined":
            return "undefined";
        case "string":
        case "number":
        case "boolean":
            break;
        default:
            param = String(param);
            break;
    }
    return encodeURIComponent(param as string | number | boolean);
}
