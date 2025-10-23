import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const FileInfo: core.serialization.ObjectSchema<serializers.FileInfo.Raw, Skyvern.FileInfo>;
export declare namespace FileInfo {
    interface Raw {
        url: string;
        checksum?: string | null;
        filename?: string | null;
        modified_at?: string | null;
    }
}
