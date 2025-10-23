import type { ValidationError } from "../../Schema.mjs";
export declare class ParseError extends Error {
    readonly errors: ValidationError[];
    constructor(errors: ValidationError[]);
}
