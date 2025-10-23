import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { Action } from "./Action.js";
import { BlockType } from "./BlockType.js";
import { RunEngine } from "./RunEngine.js";
import { WorkflowRunBlockDataSchema } from "./WorkflowRunBlockDataSchema.js";
import { WorkflowRunBlockNavigationPayload } from "./WorkflowRunBlockNavigationPayload.js";
import { WorkflowRunBlockOutput } from "./WorkflowRunBlockOutput.js";
export declare const WorkflowRunBlock: core.serialization.ObjectSchema<serializers.WorkflowRunBlock.Raw, Skyvern.WorkflowRunBlock>;
export declare namespace WorkflowRunBlock {
    interface Raw {
        workflow_run_block_id: string;
        block_workflow_run_id?: string | null;
        workflow_run_id: string;
        organization_id: string;
        description?: string | null;
        parent_workflow_run_block_id?: string | null;
        block_type: BlockType.Raw;
        label?: string | null;
        status?: string | null;
        output?: WorkflowRunBlockOutput.Raw | null;
        continue_on_failure?: boolean | null;
        failure_reason?: string | null;
        engine?: RunEngine.Raw | null;
        task_id?: string | null;
        url?: string | null;
        navigation_goal?: string | null;
        navigation_payload?: WorkflowRunBlockNavigationPayload.Raw | null;
        data_extraction_goal?: string | null;
        data_schema?: WorkflowRunBlockDataSchema.Raw | null;
        terminate_criterion?: string | null;
        complete_criterion?: string | null;
        actions?: Action.Raw[] | null;
        created_at: string;
        modified_at: string;
        include_action_history_in_verification?: boolean | null;
        duration?: number | null;
        loop_values?: unknown[] | null;
        current_value?: string | null;
        current_index?: number | null;
        recipients?: string[] | null;
        attachments?: string[] | null;
        subject?: string | null;
        body?: string | null;
    }
}
