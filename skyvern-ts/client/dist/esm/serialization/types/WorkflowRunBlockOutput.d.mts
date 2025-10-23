import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowRunBlockOutput: core.serialization.Schema<serializers.WorkflowRunBlockOutput.Raw, Skyvern.WorkflowRunBlockOutput>;
export declare namespace WorkflowRunBlockOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
