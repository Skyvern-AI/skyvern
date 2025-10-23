import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const AwsSecretParameterYaml: core.serialization.ObjectSchema<serializers.AwsSecretParameterYaml.Raw, Skyvern.AwsSecretParameterYaml>;
export declare namespace AwsSecretParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        aws_key: string;
    }
}
