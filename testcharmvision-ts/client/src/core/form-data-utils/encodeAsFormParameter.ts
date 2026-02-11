import { toQueryString } from "../url/qs.js";

export function encodeAsFormParameter(value: unknown): Record<string, string> {
    const stringified = toQueryString(value, { encode: false });

    const keyValuePairs = stringified.split("&").map((pair) => {
        const [key, value] = pair.split("=");
        return [key, value] as const;
    });

    return Object.fromEntries(keyValuePairs);
}
