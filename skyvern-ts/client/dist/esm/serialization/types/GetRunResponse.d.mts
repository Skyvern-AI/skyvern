import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { TaskRunResponse } from "./TaskRunResponse.mjs";
import { WorkflowRunResponse } from "./WorkflowRunResponse.mjs";
export declare const GetRunResponse: core.serialization.Schema<serializers.GetRunResponse.Raw, Skyvern.GetRunResponse>;
export declare namespace GetRunResponse {
    type Raw = GetRunResponse.TaskV1 | GetRunResponse.TaskV2 | GetRunResponse.OpenaiCua | GetRunResponse.AnthropicCua | GetRunResponse.UiTars | GetRunResponse.WorkflowRun;
    interface TaskV1 extends TaskRunResponse.Raw {
        run_type: "task_v1";
    }
    interface TaskV2 extends TaskRunResponse.Raw {
        run_type: "task_v2";
    }
    interface OpenaiCua extends TaskRunResponse.Raw {
        run_type: "openai_cua";
    }
    interface AnthropicCua extends TaskRunResponse.Raw {
        run_type: "anthropic_cua";
    }
    interface UiTars extends TaskRunResponse.Raw {
        run_type: "ui_tars";
    }
    interface WorkflowRun extends WorkflowRunResponse.Raw {
        run_type: "workflow_run";
    }
}
