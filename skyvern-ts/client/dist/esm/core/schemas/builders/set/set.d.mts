import { type Schema } from "../../Schema.mjs";
export declare function set<Raw, Parsed>(schema: Schema<Raw, Parsed>): Schema<Raw[], Set<Parsed>>;
