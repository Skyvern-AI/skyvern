import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const BitwardenSensitiveInformationParameter: core.serialization.ObjectSchema<serializers.BitwardenSensitiveInformationParameter.Raw, Skyvern.BitwardenSensitiveInformationParameter>;
export declare namespace BitwardenSensitiveInformationParameter {
    interface Raw {
        key: string;
        description?: string | null;
        bitwarden_sensitive_information_parameter_id: string;
        workflow_id: string;
        bitwarden_client_id_aws_secret_key: string;
        bitwarden_client_secret_aws_secret_key: string;
        bitwarden_master_password_aws_secret_key: string;
        bitwarden_collection_id: string;
        bitwarden_identity_key: string;
        bitwarden_identity_fields: string[];
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
