import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
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
