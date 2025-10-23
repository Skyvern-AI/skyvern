import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { FileInfo } from "./FileInfo.js";
export declare const BrowserSessionResponse: core.serialization.ObjectSchema<serializers.BrowserSessionResponse.Raw, Skyvern.BrowserSessionResponse>;
export declare namespace BrowserSessionResponse {
    interface Raw {
        browser_session_id: string;
        organization_id: string;
        runnable_type?: string | null;
        runnable_id?: string | null;
        timeout?: number | null;
        browser_address?: string | null;
        app_url?: string | null;
        vnc_streaming_supported?: boolean | null;
        download_path?: string | null;
        downloaded_files?: FileInfo.Raw[] | null;
        recordings?: FileInfo.Raw[] | null;
        started_at?: string | null;
        completed_at?: string | null;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
