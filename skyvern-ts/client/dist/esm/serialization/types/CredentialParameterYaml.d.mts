import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const CredentialParameterYaml: core.serialization.ObjectSchema<serializers.CredentialParameterYaml.Raw, Skyvern.CredentialParameterYaml>;
export declare namespace CredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        credential_id: string;
    }
}
