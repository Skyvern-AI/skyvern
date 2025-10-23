import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
export declare const ForLoopBlockYaml: core.serialization.ObjectSchema<serializers.ForLoopBlockYaml.Raw, Skyvern.ForLoopBlockYaml>;
export declare namespace ForLoopBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        loop_blocks: serializers.ForLoopBlockYamlLoopBlocksItem.Raw[];
        loop_over_parameter_key?: string | null;
        loop_variable_reference?: string | null;
        complete_if_empty?: boolean | null;
    }
}
