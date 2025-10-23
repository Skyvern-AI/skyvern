import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const FileEncoding: core.serialization.Schema<serializers.FileEncoding.Raw, Skyvern.FileEncoding>;
export declare namespace FileEncoding {
    type Raw = "base64" | "utf-8";
}
