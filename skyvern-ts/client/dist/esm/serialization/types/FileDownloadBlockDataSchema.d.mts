import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const FileDownloadBlockDataSchema: core.serialization.Schema<serializers.FileDownloadBlockDataSchema.Raw, Skyvern.FileDownloadBlockDataSchema>;
export declare namespace FileDownloadBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
