import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const BitwardenLoginCredentialParameter: core.serialization.ObjectSchema<serializers.BitwardenLoginCredentialParameter.Raw, Skyvern.BitwardenLoginCredentialParameter>;
export declare namespace BitwardenLoginCredentialParameter {
    interface Raw {
        key: string;
        description?: string | null;
        bitwarden_login_credential_parameter_id: string;
        workflow_id: string;
        bitwarden_client_id_aws_secret_key: string;
        bitwarden_client_secret_aws_secret_key: string;
        bitwarden_master_password_aws_secret_key: string;
        url_parameter_key?: string | null;
        bitwarden_collection_id?: string | null;
        bitwarden_item_id?: string | null;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
