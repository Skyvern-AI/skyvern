import type { ValidationError } from "../../Schema.js";
export declare class ParseError extends Error {
    readonly errors: ValidationError[];
    constructor(errors: ValidationError[]);
}
