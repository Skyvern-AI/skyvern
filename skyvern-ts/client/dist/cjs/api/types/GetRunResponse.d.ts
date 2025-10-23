import type * as Skyvern from "../index.js";
export type GetRunResponse = Skyvern.GetRunResponse.TaskV1 | Skyvern.GetRunResponse.TaskV2 | Skyvern.GetRunResponse.OpenaiCua | Skyvern.GetRunResponse.AnthropicCua | Skyvern.GetRunResponse.UiTars | Skyvern.GetRunResponse.WorkflowRun;
export declare namespace GetRunResponse {
    interface TaskV1 extends Skyvern.TaskRunResponse {
        run_type: "task_v1";
    }
    interface TaskV2 extends Skyvern.TaskRunResponse {
        run_type: "task_v2";
    }
    interface OpenaiCua extends Skyvern.TaskRunResponse {
        run_type: "openai_cua";
    }
    interface AnthropicCua extends Skyvern.TaskRunResponse {
        run_type: "anthropic_cua";
    }
    interface UiTars extends Skyvern.TaskRunResponse {
        run_type: "ui_tars";
    }
    interface WorkflowRun extends Skyvern.WorkflowRunResponse {
        run_type: "workflow_run";
    }
}
