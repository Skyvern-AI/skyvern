import { SchemaType } from "../../Schema.mjs";
import { createIdentitySchemaCreator } from "../../utils/createIdentitySchemaCreator.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
export function booleanLiteral(literal) {
    const schemaCreator = createIdentitySchemaCreator(SchemaType.BOOLEAN_LITERAL, (value, { breadcrumbsPrefix = [] } = {}) => {
        if (value === literal) {
            return {
                ok: true,
                value: literal,
            };
        }
        else {
            return {
                ok: false,
                errors: [
                    {
                        path: breadcrumbsPrefix,
                        message: getErrorMessageForIncorrectType(value, `${literal.toString()}`),
                    },
                ],
            };
        }
    });
    return schemaCreator();
}
