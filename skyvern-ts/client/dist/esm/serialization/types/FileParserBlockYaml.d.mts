import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { FileType } from "./FileType.mjs";
export declare const FileParserBlockYaml: core.serialization.ObjectSchema<serializers.FileParserBlockYaml.Raw, Skyvern.FileParserBlockYaml>;
export declare namespace FileParserBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        file_url: string;
        file_type: FileType.Raw;
        json_schema?: Record<string, unknown> | null;
    }
}
