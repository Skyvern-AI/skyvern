import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const PdfParserBlockYaml: core.serialization.ObjectSchema<serializers.PdfParserBlockYaml.Raw, Skyvern.PdfParserBlockYaml>;
export declare namespace PdfParserBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        file_url: string;
        json_schema?: Record<string, unknown> | null;
    }
}
