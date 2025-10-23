import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ThoughtType: core.serialization.Schema<serializers.ThoughtType.Raw, Skyvern.ThoughtType>;
export declare namespace ThoughtType {
    type Raw = "plan" | "metadata" | "user_goal_check" | "internal_plan" | "failure_describe";
}
