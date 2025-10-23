import type * as Skyvern from "../index.mjs";
export interface WaitBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    wait_sec: number;
    parameters?: Skyvern.WaitBlockParametersItem[];
}
