import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const FileType: core.serialization.Schema<serializers.FileType.Raw, Skyvern.FileType>;
export declare namespace FileType {
    type Raw = "csv" | "excel" | "pdf";
}
