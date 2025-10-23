import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ScriptRunResponse: core.serialization.ObjectSchema<serializers.ScriptRunResponse.Raw, Skyvern.ScriptRunResponse>;
export declare namespace ScriptRunResponse {
    interface Raw {
        ai_fallback_triggered?: boolean | null;
    }
}
