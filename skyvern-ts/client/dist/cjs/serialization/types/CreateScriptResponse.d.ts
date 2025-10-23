import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
export declare const CreateScriptResponse: core.serialization.ObjectSchema<serializers.CreateScriptResponse.Raw, Skyvern.CreateScriptResponse>;
export declare namespace CreateScriptResponse {
    interface Raw {
        script_id: string;
        version: number;
        run_id?: string | null;
        file_count: number;
        file_tree: Record<string, serializers.FileNode.Raw>;
        created_at: string;
    }
}
