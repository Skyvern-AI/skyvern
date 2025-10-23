import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const AzureVaultCredentialParameterYaml: core.serialization.ObjectSchema<serializers.AzureVaultCredentialParameterYaml.Raw, Skyvern.AzureVaultCredentialParameterYaml>;
export declare namespace AzureVaultCredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        vault_name: string;
        username_key: string;
        password_key: string;
        totp_secret_key?: string | null;
    }
}
