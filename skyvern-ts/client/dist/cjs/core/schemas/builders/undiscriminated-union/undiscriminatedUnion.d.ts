import { type Schema } from "../../Schema.js";
import type { inferParsedUnidiscriminatedUnionSchema, inferRawUnidiscriminatedUnionSchema } from "./types.js";
export declare function undiscriminatedUnion<Schemas extends [Schema<any, any>, ...Schema<any, any>[]]>(schemas: Schemas): Schema<inferRawUnidiscriminatedUnionSchema<Schemas>, inferParsedUnidiscriminatedUnionSchema<Schemas>>;
