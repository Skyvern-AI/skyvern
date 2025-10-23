import { type BaseSchema, type Schema, type SchemaOptions } from "../../Schema.mjs";
export interface SchemaUtils<Raw, Parsed> {
    nullable: () => Schema<Raw | null, Parsed | null>;
    optional: () => Schema<Raw | null | undefined, Parsed | undefined>;
    optionalNullable: () => Schema<Raw | null | undefined, Parsed | null | undefined>;
    transform: <Transformed>(transformer: SchemaTransformer<Parsed, Transformed>) => Schema<Raw, Transformed>;
    parseOrThrow: (raw: unknown, opts?: SchemaOptions) => Parsed;
    jsonOrThrow: (raw: unknown, opts?: SchemaOptions) => Raw;
}
export interface SchemaTransformer<Parsed, Transformed> {
    transform: (parsed: Parsed) => Transformed;
    untransform: (transformed: any) => Parsed;
}
export declare function getSchemaUtils<Raw, Parsed>(schema: BaseSchema<Raw, Parsed>): SchemaUtils<Raw, Parsed>;
/**
 * schema utils are defined in one file to resolve issues with circular imports
 */
export declare function nullable<Raw, Parsed>(schema: BaseSchema<Raw, Parsed>): Schema<Raw | null, Parsed | null>;
export declare function optional<Raw, Parsed>(schema: BaseSchema<Raw, Parsed>): Schema<Raw | null | undefined, Parsed | undefined>;
export declare function optionalNullable<Raw, Parsed>(schema: BaseSchema<Raw, Parsed>): Schema<Raw | null | undefined, Parsed | null | undefined>;
export declare function transform<Raw, Parsed, Transformed>(schema: BaseSchema<Raw, Parsed>, transformer: SchemaTransformer<Parsed, Transformed>): Schema<Raw, Transformed>;
