import { SchemaType } from "../../Schema.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
// https://stackoverflow.com/questions/12756159/regex-and-iso8601-formatted-datetime
const ISO_8601_REGEX = /^([+-]?\d{4}(?!\d{2}\b))((-?)((0[1-9]|1[0-2])(\3([12]\d|0[1-9]|3[01]))?|W([0-4]\d|5[0-2])(-?[1-7])?|(00[1-9]|0[1-9]\d|[12]\d{2}|3([0-5]\d|6[1-6])))([T\s]((([01]\d|2[0-3])((:?)[0-5]\d)?|24:?00)([.,]\d+(?!:))?)?(\17[0-5]\d([.,]\d+)?)?([zZ]|([+-])([01]\d|2[0-3]):?([0-5]\d)?)?)?)?$/;
export function date() {
    const baseSchema = {
        parse: (raw, { breadcrumbsPrefix = [] } = {}) => {
            if (typeof raw !== "string") {
                return {
                    ok: false,
                    errors: [
                        {
                            path: breadcrumbsPrefix,
                            message: getErrorMessageForIncorrectType(raw, "string"),
                        },
                    ],
                };
            }
            if (!ISO_8601_REGEX.test(raw)) {
                return {
                    ok: false,
                    errors: [
                        {
                            path: breadcrumbsPrefix,
                            message: getErrorMessageForIncorrectType(raw, "ISO 8601 date string"),
                        },
                    ],
                };
            }
            return {
                ok: true,
                value: new Date(raw),
            };
        },
        json: (date, { breadcrumbsPrefix = [] } = {}) => {
            if (date instanceof Date) {
                return {
                    ok: true,
                    value: date.toISOString(),
                };
            }
            else {
                return {
                    ok: false,
                    errors: [
                        {
                            path: breadcrumbsPrefix,
                            message: getErrorMessageForIncorrectType(date, "Date object"),
                        },
                    ],
                };
            }
        },
        getType: () => SchemaType.DATE,
    };
    return Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema));
}
