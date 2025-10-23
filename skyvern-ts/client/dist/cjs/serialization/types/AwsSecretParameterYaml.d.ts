import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const AwsSecretParameterYaml: core.serialization.ObjectSchema<serializers.AwsSecretParameterYaml.Raw, Skyvern.AwsSecretParameterYaml>;
export declare namespace AwsSecretParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        aws_key: string;
    }
}
