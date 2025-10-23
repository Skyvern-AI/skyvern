import { type Schema } from "../../Schema.mjs";
import type { inferParsedUnidiscriminatedUnionSchema, inferRawUnidiscriminatedUnionSchema } from "./types.mjs";
export declare function undiscriminatedUnion<Schemas extends [Schema<any, any>, ...Schema<any, any>[]]>(schemas: Schemas): Schema<inferRawUnidiscriminatedUnionSchema<Schemas>, inferParsedUnidiscriminatedUnionSchema<Schemas>>;
