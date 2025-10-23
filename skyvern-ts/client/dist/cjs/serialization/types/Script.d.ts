import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const Script: core.serialization.ObjectSchema<serializers.Script.Raw, Skyvern.Script>;
export declare namespace Script {
    interface Raw {
        script_revision_id: string;
        script_id: string;
        organization_id: string;
        run_id?: string | null;
        version: number;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
