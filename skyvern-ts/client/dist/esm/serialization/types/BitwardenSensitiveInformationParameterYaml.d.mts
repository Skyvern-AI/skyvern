import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const BitwardenSensitiveInformationParameterYaml: core.serialization.ObjectSchema<serializers.BitwardenSensitiveInformationParameterYaml.Raw, Skyvern.BitwardenSensitiveInformationParameterYaml>;
export declare namespace BitwardenSensitiveInformationParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        bitwarden_client_id_aws_secret_key: string;
        bitwarden_client_secret_aws_secret_key: string;
        bitwarden_master_password_aws_secret_key: string;
        bitwarden_collection_id: string;
        bitwarden_identity_key: string;
        bitwarden_identity_fields: string[];
    }
}
