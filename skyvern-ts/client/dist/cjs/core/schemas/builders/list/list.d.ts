import { type Schema } from "../../Schema.js";
export declare function list<Raw, Parsed>(schema: Schema<Raw, Parsed>): Schema<Raw[], Parsed[]>;
