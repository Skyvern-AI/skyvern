import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { ForLoopBlockLoopOver } from "./ForLoopBlockLoopOver.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
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
