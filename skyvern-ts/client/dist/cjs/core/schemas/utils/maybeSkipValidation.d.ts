import type { BaseSchema } from "../Schema.js";
export declare function maybeSkipValidation<S extends BaseSchema<Raw, Parsed>, Raw, Parsed>(schema: S): S;
