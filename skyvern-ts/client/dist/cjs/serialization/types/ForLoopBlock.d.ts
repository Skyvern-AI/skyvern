import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { ForLoopBlockLoopOver } from "./ForLoopBlockLoopOver.js";
import { OutputParameter } from "./OutputParameter.js";
export declare const ForLoopBlock: core.serialization.ObjectSchema<serializers.ForLoopBlock.Raw, Skyvern.ForLoopBlock>;
export declare namespace ForLoopBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        loop_blocks: serializers.ForLoopBlockLoopBlocksItem.Raw[];
        loop_over?: ForLoopBlockLoopOver.Raw | null;
        loop_variable_reference?: string | null;
        complete_if_empty?: boolean | null;
    }
}
