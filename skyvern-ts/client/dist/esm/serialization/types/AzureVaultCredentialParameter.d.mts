import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
