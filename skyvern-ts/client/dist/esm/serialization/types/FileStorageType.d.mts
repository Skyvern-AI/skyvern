import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const FileStorageType: core.serialization.Schema<serializers.FileStorageType.Raw, Skyvern.FileStorageType>;
export declare namespace FileStorageType {
    type Raw = "s3" | "azure";
}
