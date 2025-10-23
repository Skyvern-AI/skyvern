import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const CredentialParameter: core.serialization.ObjectSchema<serializers.CredentialParameter.Raw, Skyvern.CredentialParameter>;
export declare namespace CredentialParameter {
    interface Raw {
        key: string;
        description?: string | null;
        credential_parameter_id: string;
        workflow_id: string;
        credential_id: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
