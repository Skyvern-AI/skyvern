"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.Headers = void 0;
let Headers;
if (typeof globalThis.Headers !== "undefined") {
    exports.Headers = Headers = globalThis.Headers;
}
else {
    exports.Headers = Headers = class Headers {
        constructor(init) {
            this.headers = new Map();
            if (init) {
                if (init instanceof Headers) {
                    init.forEach((value, key) => this.append(key, value));
                }
                else if (Array.isArray(init)) {
                    for (const [key, value] of init) {
                        if (typeof key === "string" && typeof value === "string") {
                            this.append(key, value);
                        }
                        else {
                            throw new TypeError("Each header entry must be a [string, string] tuple");
                        }
                    }
                }
                else {
                    for (const [key, value] of Object.entries(init)) {
                        if (typeof value === "string") {
                            this.append(key, value);
                        }
                        else {
                            throw new TypeError("Header values must be strings");
                        }
                    }
                }
            }
        }
        append(name, value) {
            const key = name.toLowerCase();
            const existing = this.headers.get(key) || [];
            this.headers.set(key, [...existing, value]);
        }
        delete(name) {
            const key = name.toLowerCase();
            this.headers.delete(key);
        }
        get(name) {
            const key = name.toLowerCase();
            const values = this.headers.get(key);
            return values ? values.join(", ") : null;
        }
        has(name) {
            const key = name.toLowerCase();
            return this.headers.has(key);
        }
        set(name, value) {
            const key = name.toLowerCase();
            this.headers.set(key, [value]);
        }
        forEach(callbackfn, thisArg) {
            const boundCallback = thisArg ? callbackfn.bind(thisArg) : callbackfn;
            this.headers.forEach((values, key) => boundCallback(values.join(", "), key, this));
        }
        getSetCookie() {
            return this.headers.get("set-cookie") || [];
        }
        *entries() {
            for (const [key, values] of this.headers.entries()) {
                yield [key, values.join(", ")];
            }
        }
        *keys() {
            yield* this.headers.keys();
        }
        *values() {
            for (const values of this.headers.values()) {
                yield values.join(", ");
            }
        }
        [Symbol.iterator]() {
            return this.entries();
        }
    };
}
