import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ThoughtType: core.serialization.Schema<serializers.ThoughtType.Raw, Skyvern.ThoughtType>;
export declare namespace ThoughtType {
    type Raw = "plan" | "metadata" | "user_goal_check" | "internal_plan" | "failure_describe";
}
