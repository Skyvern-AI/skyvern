import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
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
