import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
