"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.set = set;
const Schema_js_1 = require("../../Schema.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
const maybeSkipValidation_js_1 = require("../../utils/maybeSkipValidation.js");
const index_js_1 = require("../list/index.js");
const index_js_2 = require("../schema-utils/index.js");
function set(schema) {
    const listSchema = (0, index_js_1.list)(schema);
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
                            message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(parsed, "Set"),
                        },
                    ],
                };
            }
            const jsonList = listSchema.json([...parsed], opts);
            return jsonList;
        },
        getType: () => Schema_js_1.SchemaType.SET,
    };
    return Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_2.getSchemaUtils)(baseSchema));
}
