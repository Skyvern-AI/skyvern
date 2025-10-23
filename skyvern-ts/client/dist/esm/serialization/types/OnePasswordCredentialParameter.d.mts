import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const OnePasswordCredentialParameter: core.serialization.ObjectSchema<serializers.OnePasswordCredentialParameter.Raw, Skyvern.OnePasswordCredentialParameter>;
export declare namespace OnePasswordCredentialParameter {
    interface Raw {
        key: string;
        description?: string | null;
        onepassword_credential_parameter_id: string;
        workflow_id: string;
        vault_id: string;
        item_id: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
