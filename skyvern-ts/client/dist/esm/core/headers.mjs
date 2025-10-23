export function mergeHeaders(...headersArray) {
    const result = {};
    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        }
        else if (key in result) {
            delete result[key];
        }
    }
    return result;
}
export function mergeOnlyDefinedHeaders(...headersArray) {
    const result = {};
    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        }
    }
    return result;
}
