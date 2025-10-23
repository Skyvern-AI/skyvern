"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.createIdentitySchemaCreator = createIdentitySchemaCreator;
const index_js_1 = require("../builders/schema-utils/index.js");
const maybeSkipValidation_js_1 = require("./maybeSkipValidation.js");
function createIdentitySchemaCreator(schemaType, validate) {
    return () => {
        const baseSchema = {
            parse: validate,
            json: validate,
            getType: () => schemaType,
        };
        return Object.assign(Object.assign({}, (0, maybeSkipValidation_js_1.maybeSkipValidation)(baseSchema)), (0, index_js_1.getSchemaUtils)(baseSchema));
    };
}
