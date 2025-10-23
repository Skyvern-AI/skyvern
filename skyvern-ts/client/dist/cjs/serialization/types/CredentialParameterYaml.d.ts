import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const CredentialParameterYaml: core.serialization.ObjectSchema<serializers.CredentialParameterYaml.Raw, Skyvern.CredentialParameterYaml>;
export declare namespace CredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        credential_id: string;
    }
}
