import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const FileDownloadBlockDataSchema: core.serialization.Schema<serializers.FileDownloadBlockDataSchema.Raw, Skyvern.FileDownloadBlockDataSchema>;
export declare namespace FileDownloadBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
