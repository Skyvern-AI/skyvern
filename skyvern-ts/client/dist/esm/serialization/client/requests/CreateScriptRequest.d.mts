import type * as Skyvern from "../../../api/index.mjs";
import * as core from "../../../core/index.mjs";
import type * as serializers from "../../index.mjs";
import { ScriptFileCreate } from "../../types/ScriptFileCreate.mjs";
export declare const CreateScriptRequest: core.serialization.Schema<serializers.CreateScriptRequest.Raw, Skyvern.CreateScriptRequest>;
export declare namespace CreateScriptRequest {
    interface Raw {
        workflow_id?: string | null;
        run_id?: string | null;
        files?: ScriptFileCreate.Raw[] | null;
    }
}
