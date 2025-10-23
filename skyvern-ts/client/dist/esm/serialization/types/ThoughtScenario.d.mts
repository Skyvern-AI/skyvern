import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ThoughtScenario: core.serialization.Schema<serializers.ThoughtScenario.Raw, Skyvern.ThoughtScenario>;
export declare namespace ThoughtScenario {
    type Raw = "generate_plan" | "user_goal_check" | "failure_describe" | "summarization" | "generate_metadata" | "extract_loop_values" | "generate_task_in_loop" | "generate_general_task";
}
