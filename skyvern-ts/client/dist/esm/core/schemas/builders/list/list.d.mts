import { type Schema } from "../../Schema.mjs";
export declare function list<Raw, Parsed>(schema: Schema<Raw, Parsed>): Schema<Raw[], Parsed[]>;
