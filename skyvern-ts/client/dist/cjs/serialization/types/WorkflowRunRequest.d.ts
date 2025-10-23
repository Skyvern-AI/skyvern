import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ProxyLocation } from "./ProxyLocation.js";
export declare const WorkflowRunRequest: core.serialization.ObjectSchema<serializers.WorkflowRunRequest.Raw, Skyvern.WorkflowRunRequest>;
export declare namespace WorkflowRunRequest {
    interface Raw {
        workflow_id: string;
        parameters?: Record<string, unknown> | null;
        title?: string | null;
        proxy_location?: ProxyLocation.Raw | null;
        webhook_url?: string | null;
        totp_url?: string | null;
        totp_identifier?: string | null;
        browser_session_id?: string | null;
        max_screenshot_scrolls?: number | null;
        extra_http_headers?: Record<string, string | null | undefined> | null;
        browser_address?: string | null;
        ai_fallback?: boolean | null;
        run_with?: string | null;
    }
}
