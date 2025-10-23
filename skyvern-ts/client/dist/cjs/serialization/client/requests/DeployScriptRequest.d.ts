import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
import { ScriptFileCreate } from "../../types/ScriptFileCreate.js";
export declare const DeployScriptRequest: core.serialization.Schema<serializers.DeployScriptRequest.Raw, Skyvern.DeployScriptRequest>;
export declare namespace DeployScriptRequest {
    interface Raw {
        files: ScriptFileCreate.Raw[];
    }
}
