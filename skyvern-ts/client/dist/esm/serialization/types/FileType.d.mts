import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const FileType: core.serialization.Schema<serializers.FileType.Raw, Skyvern.FileType>;
export declare namespace FileType {
    type Raw = "csv" | "excel" | "pdf";
}
