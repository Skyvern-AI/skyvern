import { SchemaType } from "../../Schema.mjs";
import { createIdentitySchemaCreator } from "../../utils/createIdentitySchemaCreator.mjs";
export const unknown = createIdentitySchemaCreator(SchemaType.UNKNOWN, (value) => ({ ok: true, value }));
