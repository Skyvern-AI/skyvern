import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const OutputParameterYaml: core.serialization.ObjectSchema<serializers.OutputParameterYaml.Raw, Skyvern.OutputParameterYaml>;
export declare namespace OutputParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
    }
}
