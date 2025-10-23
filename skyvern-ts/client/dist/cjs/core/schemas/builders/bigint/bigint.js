"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.bigint = bigint;
const Schema_js_1 = require("../../Schema.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
const maybeSkipValidation_js_1 = require("../../utils/maybeSkipValidation.js");
const index_js_1 = require("../schema-utils/index.js");
function bigint() {
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
                        message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(raw, "bigint | number"),
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
                            message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(bigint, "bigint"),
                        },
                    ],
                };
            }
            return {
                ok: true,
                value: bigint,
            };
        },
        getType: () => Schema_js_1.SchemaType.BIGINT,
    };
    return Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_1.getSchemaUtils)(baseSchema));
}
