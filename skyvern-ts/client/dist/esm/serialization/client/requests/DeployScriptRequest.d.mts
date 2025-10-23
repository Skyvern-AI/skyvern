import type * as Skyvern from "../../../api/index.mjs";
import * as core from "../../../core/index.mjs";
import type * as serializers from "../../index.mjs";
import { ScriptFileCreate } from "../../types/ScriptFileCreate.mjs";
export declare const DeployScriptRequest: core.serialization.Schema<serializers.DeployScriptRequest.Raw, Skyvern.DeployScriptRequest>;
export declare namespace DeployScriptRequest {
    interface Raw {
        files: ScriptFileCreate.Raw[];
    }
}
