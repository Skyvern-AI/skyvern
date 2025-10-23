import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
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
