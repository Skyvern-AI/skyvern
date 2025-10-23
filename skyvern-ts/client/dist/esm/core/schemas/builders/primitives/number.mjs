import { SchemaType } from "../../Schema.mjs";
import { createIdentitySchemaCreator } from "../../utils/createIdentitySchemaCreator.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
export const number = createIdentitySchemaCreator(SchemaType.NUMBER, (value, { breadcrumbsPrefix = [] } = {}) => {
    if (typeof value === "number") {
        return {
            ok: true,
            value,
        };
    }
    else {
        return {
            ok: false,
            errors: [
                {
                    path: breadcrumbsPrefix,
                    message: getErrorMessageForIncorrectType(value, "number"),
                },
            ],
        };
    }
});
