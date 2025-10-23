export interface HttpRequestBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    method?: string;
    url?: string;
    headers?: Record<string, string | undefined>;
    body?: Record<string, unknown>;
    timeout?: number;
    follow_redirects?: boolean;
    parameter_keys?: string[];
}
