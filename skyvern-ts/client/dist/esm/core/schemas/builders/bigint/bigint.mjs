import { SchemaType } from "../../Schema.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
export function bigint() {
    const baseSchema = {
        parse: (raw, { breadcrumbsPrefix = [] } = {}) => {
            if (typeof raw === "bigint") {
                return {
                    ok: true,
                    value: raw,
                };
            }
            if (typeof raw === "number") {
                return {
                    ok: true,
                    value: BigInt(raw),
                };
            }
            return {
                ok: false,
                errors: [
                    {
                        path: breadcrumbsPrefix,
                        message: getErrorMessageForIncorrectType(raw, "bigint | number"),
                    },
                ],
            };
        },
        json: (bigint, { breadcrumbsPrefix = [] } = {}) => {
            if (typeof bigint !== "bigint") {
                return {
                    ok: false,
                    errors: [
                        {
                            path: breadcrumbsPrefix,
                            message: getErrorMessageForIncorrectType(bigint, "bigint"),
                        },
                    ],
                };
            }
            return {
                ok: true,
                value: bigint,
            };
        },
        getType: () => SchemaType.BIGINT,
    };
    return Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema));
}
