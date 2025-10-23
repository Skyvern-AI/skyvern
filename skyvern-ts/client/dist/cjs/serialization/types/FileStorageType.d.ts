import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const FileStorageType: core.serialization.Schema<serializers.FileStorageType.Raw, Skyvern.FileStorageType>;
export declare namespace FileStorageType {
    type Raw = "s3" | "azure";
}
