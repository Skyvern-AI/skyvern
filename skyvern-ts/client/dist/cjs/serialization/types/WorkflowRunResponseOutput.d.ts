import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowRunResponseOutput: core.serialization.Schema<serializers.WorkflowRunResponseOutput.Raw, Skyvern.WorkflowRunResponseOutput>;
export declare namespace WorkflowRunResponseOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
