export function getHeader(headers, header) {
    for (const [headerKey, headerValue] of Object.entries(headers)) {
        if (headerKey.toLowerCase() === header.toLowerCase()) {
            return headerValue;
        }
    }
    return undefined;
}
