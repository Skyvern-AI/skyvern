import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TaskRunResponseOutput: core.serialization.Schema<serializers.TaskRunResponseOutput.Raw, Skyvern.TaskRunResponseOutput>;
export declare namespace TaskRunResponseOutput {
    type Raw = Record<string, unknown> | unknown[] | string;
}
