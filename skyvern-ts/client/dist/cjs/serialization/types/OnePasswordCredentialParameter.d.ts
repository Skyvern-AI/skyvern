import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
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
