export function join(base: string, ...segments: string[]): string {
    if (!base) {
        return "";
    }

    if (segments.length === 0) {
        return base;
    }

    if (base.includes("://")) {
        let url: URL;
        try {
            url = new URL(base);
        } catch {
            // Fallback to path joining if URL is malformed
            return joinPath(base, ...segments);
        }

        const lastSegment = segments[segments.length - 1];
        const shouldPreserveTrailingSlash = lastSegment?.endsWith("/");

        for (const segment of segments) {
            const cleanSegment = trimSlashes(segment);
            if (cleanSegment) {
                url.pathname = joinPathSegments(url.pathname, cleanSegment);
            }
        }

        if (shouldPreserveTrailingSlash && !url.pathname.endsWith("/")) {
            url.pathname += "/";
        }

        return url.toString();
    }

    return joinPath(base, ...segments);
}

function joinPath(base: string, ...segments: string[]): string {
    if (segments.length === 0) {
        return base;
    }

    let result = base;

    const lastSegment = segments[segments.length - 1];
    const shouldPreserveTrailingSlash = lastSegment?.endsWith("/");

    for (const segment of segments) {
        const cleanSegment = trimSlashes(segment);
        if (cleanSegment) {
            result = joinPathSegments(result, cleanSegment);
        }
    }

    if (shouldPreserveTrailingSlash && !result.endsWith("/")) {
        result += "/";
    }

    return result;
}

function joinPathSegments(left: string, right: string): string {
    if (left.endsWith("/")) {
        return left + right;
    }
    return `${left}/${right}`;
}

function trimSlashes(str: string): string {
    if (!str) return str;

    let start = 0;
    let end = str.length;

    if (str.startsWith("/")) start = 1;
    if (str.endsWith("/")) end = str.length - 1;

    return start === 0 && end === str.length ? str : str.slice(start, end);
}
