import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const BlockType: core.serialization.Schema<serializers.BlockType.Raw, Skyvern.BlockType>;
export declare namespace BlockType {
    type Raw = "task" | "task_v2" | "for_loop" | "code" | "text_prompt" | "download_to_s3" | "upload_to_s3" | "file_upload" | "send_email" | "file_url_parser" | "validation" | "action" | "navigation" | "extraction" | "login" | "wait" | "file_download" | "goto_url" | "pdf_parser" | "http_request";
}
