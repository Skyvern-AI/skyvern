import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { FileInfo } from "./FileInfo.js";
import { RunStatus } from "./RunStatus.js";
import { ScriptRunResponse } from "./ScriptRunResponse.js";
import { WorkflowRunRequest } from "./WorkflowRunRequest.js";
import { WorkflowRunResponseOutput } from "./WorkflowRunResponseOutput.js";
export declare const WorkflowRunResponse: core.serialization.ObjectSchema<serializers.WorkflowRunResponse.Raw, Skyvern.WorkflowRunResponse>;
export declare namespace WorkflowRunResponse {
    interface Raw {
        run_id: string;
        status: RunStatus.Raw;
        output?: WorkflowRunResponseOutput.Raw | null;
        downloaded_files?: FileInfo.Raw[] | null;
        recording_url?: string | null;
        screenshot_urls?: string[] | null;
        failure_reason?: string | null;
        created_at: string;
        modified_at: string;
        queued_at?: string | null;
        started_at?: string | null;
        finished_at?: string | null;
        app_url?: string | null;
        browser_session_id?: string | null;
        max_screenshot_scrolls?: number | null;
        script_run?: ScriptRunResponse.Raw | null;
        errors?: Record<string, unknown>[] | null;
        run_with?: string | null;
        ai_fallback?: boolean | null;
        run_request?: WorkflowRunRequest.Raw | null;
    }
}
