import type * as Skyvern from "../index.mjs";
export interface ForLoopBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    loop_blocks: Skyvern.ForLoopBlockLoopBlocksItem[];
    loop_over?: Skyvern.ForLoopBlockLoopOver;
    loop_variable_reference?: string;
    complete_if_empty?: boolean;
}
