import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { ProxyLocation } from "./ProxyLocation.mjs";
import { WorkflowDefinition } from "./WorkflowDefinition.mjs";
import { WorkflowStatus } from "./WorkflowStatus.mjs";
export declare const Workflow: core.serialization.ObjectSchema<serializers.Workflow.Raw, Skyvern.Workflow>;
export declare namespace Workflow {
    interface Raw {
        workflow_id: string;
        organization_id: string;
        title: string;
        workflow_permanent_id: string;
        version: number;
        is_saved_task: boolean;
        description?: string | null;
        workflow_definition: WorkflowDefinition.Raw;
        proxy_location?: ProxyLocation.Raw | null;
        webhook_callback_url?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        persist_browser_session?: boolean | null;
        model?: Record<string, unknown> | null;
        status?: WorkflowStatus.Raw | null;
        max_screenshot_scrolls?: number | null;
        extra_http_headers?: Record<string, string | null | undefined> | null;
        run_with?: string | null;
        ai_fallback?: boolean | null;
        cache_key?: string | null;
        run_sequentially?: boolean | null;
        sequential_key?: string | null;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
