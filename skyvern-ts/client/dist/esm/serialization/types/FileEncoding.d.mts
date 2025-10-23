import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const FileEncoding: core.serialization.Schema<serializers.FileEncoding.Raw, Skyvern.FileEncoding>;
export declare namespace FileEncoding {
    type Raw = "base64" | "utf-8";
}
