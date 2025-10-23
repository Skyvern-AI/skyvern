import type * as Skyvern from "../index.mjs";
export interface ExtractionBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    task_type?: string;
    url?: string;
    title?: string;
    engine?: Skyvern.RunEngine;
    complete_criterion?: string;
    terminate_criterion?: string;
    navigation_goal?: string;
    data_extraction_goal: string;
    data_schema?: ExtractionBlock.DataSchema;
    error_code_mapping?: Record<string, string | undefined>;
    max_retries?: number;
    max_steps_per_run?: number;
    parameters?: Skyvern.ExtractionBlockParametersItem[];
    complete_on_download?: boolean;
    download_suffix?: string;
    totp_verification_url?: string;
    totp_identifier?: string;
    cache_actions?: boolean;
    complete_verification?: boolean;
    include_action_history_in_verification?: boolean;
    download_timeout?: number;
}
export declare namespace ExtractionBlock {
    type DataSchema = Record<string, unknown> | unknown[] | string;
}
