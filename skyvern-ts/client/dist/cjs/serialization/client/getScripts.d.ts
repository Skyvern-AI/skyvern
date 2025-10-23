import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { Script } from "../types/Script.js";
export declare const Response: core.serialization.Schema<serializers.getScripts.Response.Raw, Skyvern.Script[]>;
export declare namespace Response {
    type Raw = Script.Raw[];
}
