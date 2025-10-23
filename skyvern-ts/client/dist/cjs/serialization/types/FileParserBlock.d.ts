import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { FileType } from "./FileType.js";
import { OutputParameter } from "./OutputParameter.js";
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
