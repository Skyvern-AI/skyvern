import { SchemaType } from "../../Schema.mjs";
import { getErrorMessageForIncorrectType } from "../../utils/getErrorMessageForIncorrectType.mjs";
import { maybeSkipValidation } from "../../utils/maybeSkipValidation.mjs";
import { list } from "../list/index.mjs";
import { getSchemaUtils } from "../schema-utils/index.mjs";
export function set(schema) {
    const listSchema = list(schema);
    const baseSchema = {
        parse: (raw, opts) => {
            const parsedList = listSchema.parse(raw, opts);
            if (parsedList.ok) {
                return {
                    ok: true,
                    value: new Set(parsedList.value),
                };
            }
            else {
                return parsedList;
            }
        },
        json: (parsed, opts) => {
            var _a;
            if (!(parsed instanceof Set)) {
                return {
                    ok: false,
                    errors: [
                        {
                            path: (_a = opts === null || opts === void 0 ? void 0 : opts.breadcrumbsPrefix) !== null && _a !== void 0 ? _a : [],
                            message: getErrorMessageForIncorrectType(parsed, "Set"),
                        },
                    ],
                };
            }
            const jsonList = listSchema.json([...parsed], opts);
            return jsonList;
        },
        getType: () => SchemaType.SET,
    };
    return Object.assign(Object.assign({}, maybeSkipValidation(baseSchema)), getSchemaUtils(baseSchema));
}
