import type * as Skyvern from "../index.js";
export interface ExtractionBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    data_extraction_goal: string;
    url?: string;
    title?: string;
    engine?: Skyvern.RunEngine;
    data_schema?: ExtractionBlockYaml.DataSchema;
    max_retries?: number;
    max_steps_per_run?: number;
    parameter_keys?: string[];
    cache_actions?: boolean;
    disable_cache?: boolean;
}
export declare namespace ExtractionBlockYaml {
    type DataSchema = Record<string, unknown> | unknown[] | string;
}
