"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.number = void 0;
const Schema_js_1 = require("../../Schema.js");
const createIdentitySchemaCreator_js_1 = require("../../utils/createIdentitySchemaCreator.js");
const getErrorMessageForIncorrectType_js_1 = require("../../utils/getErrorMessageForIncorrectType.js");
exports.number = (0, createIdentitySchemaCreator_js_1.createIdentitySchemaCreator)(Schema_js_1.SchemaType.NUMBER, (value, { breadcrumbsPrefix = [] } = {}) => {
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
                    message: (0, getErrorMessageForIncorrectType_js_1.getErrorMessageForIncorrectType)(value, "number"),
                },
            ],
        };
    }
});
