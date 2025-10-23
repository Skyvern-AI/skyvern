"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.toQueryString = toQueryString;
const defaultQsOptions = {
    arrayFormat: "indices",
    encode: true,
};
function encodeValue(value, shouldEncode) {
    if (value === undefined) {
        return "";
    }
    if (value === null) {
        return "";
    }
    const stringValue = String(value);
    return shouldEncode ? encodeURIComponent(stringValue) : stringValue;
}
function stringifyObject(obj, prefix = "", options) {
    const parts = [];
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
                    parts.push(...stringifyObject(item, arrayKey, options));
                }
                else {
                    const arrayKey = options.arrayFormat === "indices" ? `${fullKey}[${i}]` : fullKey;
                    const encodedKey = options.encode ? encodeURIComponent(arrayKey) : arrayKey;
                    parts.push(`${encodedKey}=${encodeValue(item, options.encode)}`);
                }
            }
        }
        else if (typeof value === "object" && value !== null) {
            if (Object.keys(value).length === 0) {
                continue;
            }
            parts.push(...stringifyObject(value, fullKey, options));
        }
        else {
            const encodedKey = options.encode ? encodeURIComponent(fullKey) : fullKey;
            parts.push(`${encodedKey}=${encodeValue(value, options.encode)}`);
        }
    }
    return parts;
}
function toQueryString(obj, options) {
    if (obj == null || typeof obj !== "object") {
        return "";
    }
    const parts = stringifyObject(obj, "", Object.assign(Object.assign({}, defaultQsOptions), options));
    return parts.join("&");
}
