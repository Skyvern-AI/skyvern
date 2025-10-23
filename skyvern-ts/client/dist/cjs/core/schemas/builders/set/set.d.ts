import { type Schema } from "../../Schema.js";
export declare function set<Raw, Parsed>(schema: Schema<Raw, Parsed>): Schema<Raw[], Set<Parsed>>;
