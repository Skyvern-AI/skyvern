import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ValidationBlockYaml: core.serialization.ObjectSchema<serializers.ValidationBlockYaml.Raw, Skyvern.ValidationBlockYaml>;
export declare namespace ValidationBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        complete_criterion?: string | null;
        terminate_criterion?: string | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        parameter_keys?: string[] | null;
        disable_cache?: boolean | null;
    }
}
