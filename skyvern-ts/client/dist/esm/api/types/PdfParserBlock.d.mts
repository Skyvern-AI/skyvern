import type * as Skyvern from "../index.mjs";
/**
 * DEPRECATED: Use FileParserBlock with file_type=FileType.PDF instead.
 * This block will be removed in a future version.
 */
export interface PdfParserBlock {
    label: string;
    output_parameter: Skyvern.OutputParameter;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    disable_cache?: boolean;
    file_url: string;
    json_schema?: Record<string, unknown>;
}
