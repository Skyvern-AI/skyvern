"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.enum_ = enum_;
const Schema_js_1 = require("../../Schema.js");
const createIdentitySchemaCreator_js_1 = require("../../utils/createIdentitySchemaCreator.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
function enum_(values) {
    const validValues = new Set(values);
    const schemaCreator = (0, createIdentitySchemaCreator_js_1.createIdentitySchemaCreator)(Schema_js_1.SchemaType.ENUM, (value, { allowUnrecognizedEnumValues, breadcrumbsPrefix = [] } = {}) => {
        if (typeof value !== "string") {
            return {
                ok: false,
                errors: [
                    {
                        path: breadcrumbsPrefix,
                        message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, "string"),
                    },
                ],
            };
        }
        if (!validValues.has(value) && !allowUnrecognizedEnumValues) {
            return {
                ok: false,
                errors: [
                    {
                        path: breadcrumbsPrefix,
                        message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, "enum"),
                    },
                ],
            };
        }
        return {
            ok: true,
            value: value,
        };
    });
    return schemaCreator();
}
