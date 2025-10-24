interface QueryStringOptions {
    arrayFormat?: "indices" | "repeat";
    encode?: boolean;
}

const defaultQsOptions: Required<QueryStringOptions> = {
    arrayFormat: "indices",
    encode: true,
} as const;

function encodeValue(value: unknown, shouldEncode: boolean): string {
    if (value === undefined) {
        return "";
    }
    if (value === null) {
        return "";
    }
    const stringValue = String(value);
    return shouldEncode ? encodeURIComponent(stringValue) : stringValue;
}

function stringifyObject(obj: Record<string, unknown>, prefix = "", options: Required<QueryStringOptions>): string[] {
    const parts: string[] = [];

    for (const [key, value] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}[${key}]` : key;

        if (value === undefined) {
            continue;
        }

        if (Array.isArray(value)) {
            if (value.length === 0) {
                continue;
            }
            for (let i = 0; i < value.length; i++) {
                const item = value[i];
                if (item === undefined) {
                    continue;
                }
                if (typeof item === "object" && !Array.isArray(item) && item !== null) {
                    const arrayKey = options.arrayFormat === "indices" ? `${fullKey}[${i}]` : fullKey;
                    parts.push(...stringifyObject(item as Record<string, unknown>, arrayKey, options));
                } else {
                    const arrayKey = options.arrayFormat === "indices" ? `${fullKey}[${i}]` : fullKey;
                    const encodedKey = options.encode ? encodeURIComponent(arrayKey) : arrayKey;
                    parts.push(`${encodedKey}=${encodeValue(item, options.encode)}`);
                }
            }
        } else if (typeof value === "object" && value !== null) {
            if (Object.keys(value as Record<string, unknown>).length === 0) {
                continue;
            }
            parts.push(...stringifyObject(value as Record<string, unknown>, fullKey, options));
        } else {
            const encodedKey = options.encode ? encodeURIComponent(fullKey) : fullKey;
            parts.push(`${encodedKey}=${encodeValue(value, options.encode)}`);
        }
    }

    return parts;
}

export function toQueryString(obj: unknown, options?: QueryStringOptions): string {
    if (obj == null || typeof obj !== "object") {
        return "";
    }

    const parts = stringifyObject(obj as Record<string, unknown>, "", {
        ...defaultQsOptions,
        ...options,
    });
    return parts.join("&");
}
