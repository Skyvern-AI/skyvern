export declare const ThoughtScenario: {
    readonly GeneratePlan: "generate_plan";
    readonly UserGoalCheck: "user_goal_check";
    readonly FailureDescribe: "failure_describe";
    readonly Summarization: "summarization";
    readonly GenerateMetadata: "generate_metadata";
    readonly ExtractLoopValues: "extract_loop_values";
    readonly GenerateTaskInLoop: "generate_task_in_loop";
    readonly GenerateGeneralTask: "generate_general_task";
};
export type ThoughtScenario = (typeof ThoughtScenario)[keyof typeof ThoughtScenario];
