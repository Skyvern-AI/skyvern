import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { TaskRunResponse } from "./TaskRunResponse.js";
import { WorkflowRunResponse } from "./WorkflowRunResponse.js";
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
