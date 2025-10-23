import type * as Skyvern from "../index.js";
export interface ContextParameter {
    key: string;
    description?: string;
    source: Skyvern.ContextParameterSource;
    value?: ContextParameter.Value;
}
export declare namespace ContextParameter {
    type Value = string | number | number | boolean | Record<string, unknown> | unknown[];
}
