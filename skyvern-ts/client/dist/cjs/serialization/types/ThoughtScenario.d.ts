import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ThoughtScenario: core.serialization.Schema<serializers.ThoughtScenario.Raw, Skyvern.ThoughtScenario>;
export declare namespace ThoughtScenario {
    type Raw = "generate_plan" | "user_goal_check" | "failure_describe" | "summarization" | "generate_metadata" | "extract_loop_values" | "generate_task_in_loop" | "generate_general_task";
}
