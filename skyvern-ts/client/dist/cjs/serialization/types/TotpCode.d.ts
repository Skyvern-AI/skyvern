import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { OtpType } from "./OtpType.js";
export declare const TotpCode: core.serialization.ObjectSchema<serializers.TotpCode.Raw, Skyvern.TotpCode>;
export declare namespace TotpCode {
    interface Raw {
        totp_identifier: string;
        task_id?: string | null;
        workflow_id?: string | null;
        workflow_run_id?: string | null;
        source?: string | null;
        content: string;
        expired_at?: string | null;
        totp_code_id: string;
        code: string;
        organization_id: string;
        created_at: string;
        modified_at: string;
        otp_type?: OtpType.Raw | null;
    }
}
