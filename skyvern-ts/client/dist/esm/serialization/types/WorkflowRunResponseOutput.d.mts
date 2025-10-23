import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowRunResponseOutput: core.serialization.Schema<serializers.WorkflowRunResponseOutput.Raw, Skyvern.WorkflowRunResponseOutput>;
export declare namespace WorkflowRunResponseOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
