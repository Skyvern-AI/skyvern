import type * as Skyvern from "../index.mjs";
export interface HttpRequestBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    method?: string;
    url?: string;
    headers?: Record<string, string | undefined>;
    body?: Record<string, unknown>;
    timeout?: number;
    follow_redirects?: boolean;
    parameters?: Skyvern.HttpRequestBlockParametersItem[];
}
