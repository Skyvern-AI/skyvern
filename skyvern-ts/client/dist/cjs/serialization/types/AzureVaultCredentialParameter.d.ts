import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const AzureVaultCredentialParameter: core.serialization.ObjectSchema<serializers.AzureVaultCredentialParameter.Raw, Skyvern.AzureVaultCredentialParameter>;
export declare namespace AzureVaultCredentialParameter {
    interface Raw {
        key: string;
        description?: string | null;
        azure_vault_credential_parameter_id: string;
        workflow_id: string;
        vault_name: string;
        username_key: string;
        password_key: string;
        totp_secret_key?: string | null;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
