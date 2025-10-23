import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { OutputParameter } from "./OutputParameter.js";
export declare const PdfParserBlock: core.serialization.ObjectSchema<serializers.PdfParserBlock.Raw, Skyvern.PdfParserBlock>;
export declare namespace PdfParserBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        file_url: string;
        json_schema?: Record<string, unknown> | null;
    }
}
