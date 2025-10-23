import type { BaseSchema } from "../Schema.mjs";
export declare function maybeSkipValidation<S extends BaseSchema<Raw, Parsed>, Raw, Parsed>(schema: S): S;
