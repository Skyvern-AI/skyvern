import type { ValidationError } from "../../Schema.mjs";
export declare class JsonError extends Error {
    readonly errors: ValidationError[];
    constructor(errors: ValidationError[]);
}
