import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ProxyLocation } from "./ProxyLocation.js";
import { WorkflowDefinitionYaml } from "./WorkflowDefinitionYaml.js";
import { WorkflowStatus } from "./WorkflowStatus.js";
export declare const WorkflowCreateYamlRequest: core.serialization.ObjectSchema<serializers.WorkflowCreateYamlRequest.Raw, Skyvern.WorkflowCreateYamlRequest>;
export declare namespace WorkflowCreateYamlRequest {
    interface Raw {
        title: string;
        description?: string | null;
        proxy_location?: ProxyLocation.Raw | null;
        webhook_callback_url?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        persist_browser_session?: boolean | null;
        model?: Record<string, unknown> | null;
        workflow_definition: WorkflowDefinitionYaml.Raw;
        is_saved_task?: boolean | null;
        max_screenshot_scrolls?: number | null;
        extra_http_headers?: Record<string, string | null | undefined> | null;
        status?: WorkflowStatus.Raw | null;
        run_with?: string | null;
        ai_fallback?: boolean | null;
        cache_key?: string | null;
        run_sequentially?: boolean | null;
        sequential_key?: string | null;
    }
}
