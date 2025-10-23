import { object } from "./object.mjs";
export function objectWithoutOptionalProperties(schemas) {
    return object(schemas);
}
