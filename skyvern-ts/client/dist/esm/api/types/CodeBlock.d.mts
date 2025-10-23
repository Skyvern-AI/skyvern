import type * as Skyvern from "../index.mjs";
export interface CodeBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    code: string;
    parameters?: Skyvern.CodeBlockParametersItem[];
}
