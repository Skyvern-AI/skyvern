import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
import { ScriptFileCreate } from "../../types/ScriptFileCreate.js";
export declare const CreateScriptRequest: core.serialization.Schema<serializers.CreateScriptRequest.Raw, Skyvern.CreateScriptRequest>;
export declare namespace CreateScriptRequest {
    interface Raw {
        workflow_id?: string | null;
        run_id?: string | null;
        files?: ScriptFileCreate.Raw[] | null;
    }
}
