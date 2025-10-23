import { SchemaType } from "../../Schema.mjs";
import { createIdentitySchemaCreator } from "../../utils/createIdentitySchemaCreator.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
export const boolean = createIdentitySchemaCreator(SchemaType.BOOLEAN, (value, { breadcrumbsPrefix = [] } = {}) => {
    if (typeof value === "boolean") {
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
                    message: getErrorMessageForIncorrectType(value, "boolean"),
                },
            ],
        };
    }
});
