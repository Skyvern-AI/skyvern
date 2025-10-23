import type { ObjectSchema } from "../object/types.js";
import { type SchemaGetter } from "./lazy.js";
export declare function lazyObject<Raw, Parsed>(getter: SchemaGetter<ObjectSchema<Raw, Parsed>>): ObjectSchema<Raw, Parsed>;
