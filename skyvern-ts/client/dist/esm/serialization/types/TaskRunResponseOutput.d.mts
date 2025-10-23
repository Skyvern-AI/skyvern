import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const TaskRunResponseOutput: core.serialization.Schema<serializers.TaskRunResponseOutput.Raw, Skyvern.TaskRunResponseOutput>;
export declare namespace TaskRunResponseOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
