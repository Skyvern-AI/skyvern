"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.booleanLiteral = booleanLiteral;
const Schema_js_1 = require("../../Schema.js");
const createIdentitySchemaCreator_js_1 = require("../../utils/createIdentitySchemaCreator.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
function booleanLiteral(literal) {
    const schemaCreator = (0, createIdentitySchemaCreator_js_1.createIdentitySchemaCreator)(Schema_js_1.SchemaType.BOOLEAN_LITERAL, (value, { breadcrumbsPrefix = [] } = {}) => {
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
                        message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, `${literal.toString()}`),
                    },
                ],
            };
        }
    });
    return schemaCreator();
}
