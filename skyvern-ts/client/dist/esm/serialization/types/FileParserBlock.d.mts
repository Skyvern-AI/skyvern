import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { FileType } from "./FileType.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const FileParserBlock: core.serialization.ObjectSchema<serializers.FileParserBlock.Raw, Skyvern.FileParserBlock>;
export declare namespace FileParserBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        file_url: string;
        file_type: FileType.Raw;
        json_schema?: Record<string, unknown> | null;
    }
}
