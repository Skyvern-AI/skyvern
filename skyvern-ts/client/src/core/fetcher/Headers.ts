let Headers: typeof globalThis.Headers;

if (typeof globalThis.Headers !== "undefined") {
    Headers = globalThis.Headers;
} else {
    Headers = class Headers implements Headers {
        private headers: Map<string, string[]>;

        constructor(init?: HeadersInit) {
            this.headers = new Map();

            if (init) {
                if (init instanceof Headers) {
                    init.forEach((value, key) => this.append(key, value));
                } else if (Array.isArray(init)) {
                    for (const [key, value] of init) {
                        if (typeof key === "string" && typeof value === "string") {
                            this.append(key, value);
                        } else {
                            throw new TypeError("Each header entry must be a [string, string] tuple");
                        }
                    }
                } else {
                    for (const [key, value] of Object.entries(init)) {
                        if (typeof value === "string") {
                            this.append(key, value);
                        } else {
                            throw new TypeError("Header values must be strings");
                        }
                    }
                }
            }
        }

        append(name: string, value: string): void {
            const key = name.toLowerCase();
            const existing = this.headers.get(key) || [];
            this.headers.set(key, [...existing, value]);
        }

        delete(name: string): void {
            const key = name.toLowerCase();
            this.headers.delete(key);
        }

        get(name: string): string | null {
            const key = name.toLowerCase();
            const values = this.headers.get(key);
            return values ? values.join(", ") : null;
        }

        has(name: string): boolean {
            const key = name.toLowerCase();
            return this.headers.has(key);
        }

        set(name: string, value: string): void {
            const key = name.toLowerCase();
            this.headers.set(key, [value]);
        }

        forEach(callbackfn: (value: string, key: string, parent: Headers) => void, thisArg?: unknown): void {
            const boundCallback = thisArg ? callbackfn.bind(thisArg) : callbackfn;
            this.headers.forEach((values, key) => boundCallback(values.join(", "), key, this));
        }

        getSetCookie(): string[] {
            return this.headers.get("set-cookie") || [];
        }

        *entries(): HeadersIterator<[string, string]> {
            for (const [key, values] of this.headers.entries()) {
                yield [key, values.join(", ")];
            }
        }

        *keys(): HeadersIterator<string> {
            yield* this.headers.keys();
        }

        *values(): HeadersIterator<string> {
            for (const values of this.headers.values()) {
                yield values.join(", ");
            }
        }

        [Symbol.iterator](): HeadersIterator<[string, string]> {
            return this.entries();
        }
    };
}

export { Headers };
