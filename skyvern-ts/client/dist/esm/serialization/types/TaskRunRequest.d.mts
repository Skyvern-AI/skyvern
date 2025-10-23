import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { ProxyLocation } from "./ProxyLocation.mjs";
import { RunEngine } from "./RunEngine.mjs";
import { TaskRunRequestDataExtractionSchema } from "./TaskRunRequestDataExtractionSchema.mjs";
export declare const TaskRunRequest: core.serialization.ObjectSchema<serializers.TaskRunRequest.Raw, Skyvern.TaskRunRequest>;
export declare namespace TaskRunRequest {
    interface Raw {
        prompt: string;
        url?: string | null;
        engine?: RunEngine.Raw | null;
        title?: string | null;
        proxy_location?: ProxyLocation.Raw | null;
        data_extraction_schema?: TaskRunRequestDataExtractionSchema.Raw | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_steps?: number | null;
        webhook_url?: string | null;
        totp_identifier?: string | null;
        totp_url?: string | null;
        browser_session_id?: string | null;
        model?: Record<string, unknown> | null;
        extra_http_headers?: Record<string, string | null | undefined> | null;
        publish_workflow?: boolean | null;
        include_action_history_in_verification?: boolean | null;
        max_screenshot_scrolls?: number | null;
        browser_address?: string | null;
    }
}
