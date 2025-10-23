import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { FileStorageType } from "./FileStorageType.js";
import { OutputParameter } from "./OutputParameter.js";
export declare const FileUploadBlock: core.serialization.ObjectSchema<serializers.FileUploadBlock.Raw, Skyvern.FileUploadBlock>;
export declare namespace FileUploadBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        storage_type?: FileStorageType.Raw | null;
        s3_bucket?: string | null;
        aws_access_key_id?: string | null;
        aws_secret_access_key?: string | null;
        region_name?: string | null;
        azure_storage_account_name?: string | null;
        azure_storage_account_key?: string | null;
        azure_blob_container_name?: string | null;
        path?: string | null;
    }
}
