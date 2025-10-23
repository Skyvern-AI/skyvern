import type { BaseObjectSchema, inferObjectSchemaFromPropertySchemas, ObjectUtils, PropertySchemas } from "./types.mjs";
export declare function object<ParsedKeys extends string, T extends PropertySchemas<ParsedKeys>>(schemas: T): inferObjectSchemaFromPropertySchemas<T>;
export declare function getObjectUtils<Raw, Parsed>(schema: BaseObjectSchema<Raw, Parsed>): ObjectUtils<Raw, Parsed>;
