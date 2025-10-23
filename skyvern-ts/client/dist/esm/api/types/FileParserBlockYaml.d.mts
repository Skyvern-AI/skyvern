import type * as Skyvern from "../index.mjs";
export interface FileParserBlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    file_url: string;
    file_type: Skyvern.FileType;
    json_schema?: Record<string, unknown>;
}
