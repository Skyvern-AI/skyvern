import type * as Skyvern from "../index.mjs";
export interface WorkflowRunTimeline {
    type: Skyvern.WorkflowRunTimelineType;
    block?: Skyvern.WorkflowRunBlock;
    thought?: Skyvern.Thought;
    children?: Skyvern.WorkflowRunTimeline[];
    created_at: string;
    modified_at: string;
}
