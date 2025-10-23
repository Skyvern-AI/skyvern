import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
