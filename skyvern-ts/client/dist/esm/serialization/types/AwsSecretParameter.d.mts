import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const AwsSecretParameter: core.serialization.ObjectSchema<serializers.AwsSecretParameter.Raw, Skyvern.AwsSecretParameter>;
export declare namespace AwsSecretParameter {
    interface Raw {
        key: string;
        description?: string | null;
        aws_secret_parameter_id: string;
        workflow_id: string;
        aws_key: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
