import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const FileInfo: core.serialization.ObjectSchema<serializers.FileInfo.Raw, Skyvern.FileInfo>;
export declare namespace FileInfo {
    interface Raw {
        url: string;
        checksum?: string | null;
        filename?: string | null;
        modified_at?: string | null;
    }
}
