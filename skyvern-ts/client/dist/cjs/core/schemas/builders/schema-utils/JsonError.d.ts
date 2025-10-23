import type { ValidationError } from "../../Schema.js";
export declare class JsonError extends Error {
    readonly errors: ValidationError[];
    constructor(errors: ValidationError[]);
}
