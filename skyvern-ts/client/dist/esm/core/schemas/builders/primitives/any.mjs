import { SchemaType } from "../../Schema.mjs";
import { createIdentitySchemaCreator } from "../../utils/createIdentitySchemaCreator.mjs";
export const any = createIdentitySchemaCreator(SchemaType.ANY, (value) => ({
    ok: true,
    value,
}));
