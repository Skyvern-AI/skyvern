import type * as Skyvern from "../../../api/index.mjs";
import * as core from "../../../core/index.mjs";
import type * as serializers from "../../index.mjs";
export declare const TotpCodeCreate: core.serialization.Schema<serializers.TotpCodeCreate.Raw, Skyvern.TotpCodeCreate>;
export declare namespace TotpCodeCreate {
    interface Raw {
        totp_identifier: string;
        task_id?: string | null;
        workflow_id?: string | null;
        workflow_run_id?: string | null;
        source?: string | null;
        content: string;
        expired_at?: string | null;
    }
}
