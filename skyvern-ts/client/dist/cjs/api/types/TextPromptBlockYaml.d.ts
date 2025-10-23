export interface TextPromptBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    llm_key?: string;
    prompt: string;
    parameter_keys?: string[];
    json_schema?: Record<string, unknown>;
}
