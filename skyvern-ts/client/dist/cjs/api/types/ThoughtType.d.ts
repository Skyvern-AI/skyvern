export declare const ThoughtType: {
    readonly Plan: "plan";
    readonly Metadata: "metadata";
    readonly UserGoalCheck: "user_goal_check";
    readonly InternalPlan: "internal_plan";
    readonly FailureDescribe: "failure_describe";
};
export type ThoughtType = (typeof ThoughtType)[keyof typeof ThoughtType];
