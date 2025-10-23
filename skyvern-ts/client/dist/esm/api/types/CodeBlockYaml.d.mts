export interface CodeBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    code: string;
    parameter_keys?: string[];
}
