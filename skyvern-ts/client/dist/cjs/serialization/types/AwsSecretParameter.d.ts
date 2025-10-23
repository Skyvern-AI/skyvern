import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
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
