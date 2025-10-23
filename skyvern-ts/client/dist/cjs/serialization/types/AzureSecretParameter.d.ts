import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const AzureSecretParameter: core.serialization.ObjectSchema<serializers.AzureSecretParameter.Raw, Skyvern.AzureSecretParameter>;
export declare namespace AzureSecretParameter {
    interface Raw {
        key: string;
        description?: string | null;
        azure_secret_parameter_id: string;
        workflow_id: string;
        azure_key: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
