import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
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
