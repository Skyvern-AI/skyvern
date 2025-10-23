import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const BitwardenLoginCredentialParameterYaml: core.serialization.ObjectSchema<serializers.BitwardenLoginCredentialParameterYaml.Raw, Skyvern.BitwardenLoginCredentialParameterYaml>;
export declare namespace BitwardenLoginCredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        bitwarden_client_id_aws_secret_key: string;
        bitwarden_client_secret_aws_secret_key: string;
        bitwarden_master_password_aws_secret_key: string;
        url_parameter_key?: string | null;
        bitwarden_collection_id?: string | null;
        bitwarden_item_id?: string | null;
    }
}
