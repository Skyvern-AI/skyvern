import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const OnePasswordCredentialParameterYaml: core.serialization.ObjectSchema<serializers.OnePasswordCredentialParameterYaml.Raw, Skyvern.OnePasswordCredentialParameterYaml>;
export declare namespace OnePasswordCredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        vault_id: string;
        item_id: string;
    }
}
