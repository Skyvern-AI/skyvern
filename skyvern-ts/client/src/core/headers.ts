export function mergeHeaders<THeaderValue>(
    ...headersArray: (Record<string, THeaderValue> | null | undefined)[]
): Record<string, string | THeaderValue> {
    const result: Record<string, THeaderValue> = {};

    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        } else if (key in result) {
            delete result[key];
        }
    }

    return result;
}

export function mergeOnlyDefinedHeaders<THeaderValue>(
    ...headersArray: (Record<string, THeaderValue> | null | undefined)[]
): Record<string, THeaderValue> {
    const result: Record<string, THeaderValue> = {};

    for (const [key, value] of headersArray
        .filter((headers) => headers != null)
        .flatMap((headers) => Object.entries(headers))) {
        if (value != null) {
            result[key] = value;
        }
    }

    return result;
}
