export declare const WorkflowRunTimelineType: {
    readonly Thought: "thought";
    readonly Block: "block";
};
export type WorkflowRunTimelineType = (typeof WorkflowRunTimelineType)[keyof typeof WorkflowRunTimelineType];
