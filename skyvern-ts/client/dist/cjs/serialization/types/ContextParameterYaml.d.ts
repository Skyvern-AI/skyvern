import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ContextParameterYaml: core.serialization.ObjectSchema<serializers.ContextParameterYaml.Raw, Skyvern.ContextParameterYaml>;
export declare namespace ContextParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        source_parameter_key: string;
    }
}
