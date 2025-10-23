import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { FileStorageType } from "./FileStorageType.mjs";
export declare const FileUploadBlockYaml: core.serialization.ObjectSchema<serializers.FileUploadBlockYaml.Raw, Skyvern.FileUploadBlockYaml>;
export declare namespace FileUploadBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        storage_type?: FileStorageType.Raw | null;
        s3_bucket?: string | null;
        aws_access_key_id?: string | null;
        aws_secret_access_key?: string | null;
        region_name?: string | null;
        azure_storage_account_name?: string | null;
        azure_storage_account_key?: string | null;
        azure_blob_container_name?: string | null;
        azure_folder_path?: string | null;
        path?: string | null;
    }
}
