"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.any = void 0;
const Schema_js_1 = require("../../Schema.js");
const createIdentitySchemaCreator_js_1 = require("../../utils/createIdentitySchemaCreator.js");
exports.any = (0, createIdentitySchemaCreator_js_1.createIdentitySchemaCreator)(Schema_js_1.SchemaType.ANY, (value) => ({
    ok: true,
    value,
}));
