import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const OutputParameterYaml: core.serialization.ObjectSchema<serializers.OutputParameterYaml.Raw, Skyvern.OutputParameterYaml>;
export declare namespace OutputParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
    }
}
