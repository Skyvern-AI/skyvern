import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { Script } from "../types/Script.mjs";
export declare const Response: core.serialization.Schema<serializers.getScripts.Response.Raw, Skyvern.Script[]>;
export declare namespace Response {
    type Raw = Script.Raw[];
}
