import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const OnePasswordCredentialParameterYaml: core.serialization.ObjectSchema<serializers.OnePasswordCredentialParameterYaml.Raw, Skyvern.OnePasswordCredentialParameterYaml>;
export declare namespace OnePasswordCredentialParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        vault_id: string;
        item_id: string;
    }
}
