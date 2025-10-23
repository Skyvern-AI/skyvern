export interface PdfParserBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    file_url: string;
    json_schema?: Record<string, unknown>;
}
