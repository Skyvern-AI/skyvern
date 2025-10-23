import type { BaseSchema, Schema } from "../../Schema.js";
export type SchemaGetter<SchemaType extends Schema<any, any>> = () => SchemaType;
export declare function lazy<Raw, Parsed>(getter: SchemaGetter<Schema<Raw, Parsed>>): Schema<Raw, Parsed>;
export declare function constructLazyBaseSchema<Raw, Parsed>(getter: SchemaGetter<Schema<Raw, Parsed>>): BaseSchema<Raw, Parsed>;
export declare function getMemoizedSchema<SchemaType extends Schema<any, any>>(getter: SchemaGetter<SchemaType>): SchemaType;
