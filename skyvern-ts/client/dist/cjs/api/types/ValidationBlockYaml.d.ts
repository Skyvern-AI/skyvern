export interface ValidationBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    complete_criterion?: string;
    terminate_criterion?: string;
    error_code_mapping?: Record<string, string | undefined>;
    parameter_keys?: string[];
    disable_cache?: boolean;
}
