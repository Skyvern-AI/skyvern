import type { inferParsed, inferRaw, Schema } from "../../Schema.mjs";
export type UndiscriminatedUnionSchema<Schemas extends [...Schema[]]> = Schema<inferRawUnidiscriminatedUnionSchema<Schemas>, inferParsedUnidiscriminatedUnionSchema<Schemas>>;
export type inferRawUnidiscriminatedUnionSchema<Schemas extends [...Schema[]]> = inferRaw<Schemas[number]>;
export type inferParsedUnidiscriminatedUnionSchema<Schemas extends [...Schema[]]> = inferParsed<Schemas[number]>;
