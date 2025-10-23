import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ContextParameterYaml: core.serialization.ObjectSchema<serializers.ContextParameterYaml.Raw, Skyvern.ContextParameterYaml>;
export declare namespace ContextParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        source_parameter_key: string;
    }
}
