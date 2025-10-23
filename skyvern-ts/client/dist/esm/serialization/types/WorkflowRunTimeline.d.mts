import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { Thought } from "./Thought.mjs";
import { WorkflowRunBlock } from "./WorkflowRunBlock.mjs";
import { WorkflowRunTimelineType } from "./WorkflowRunTimelineType.mjs";
export declare const WorkflowRunTimeline: core.serialization.ObjectSchema<serializers.WorkflowRunTimeline.Raw, Skyvern.WorkflowRunTimeline>;
export declare namespace WorkflowRunTimeline {
    interface Raw {
        type: WorkflowRunTimelineType.Raw;
        block?: WorkflowRunBlock.Raw | null;
        thought?: Thought.Raw | null;
        children?: serializers.WorkflowRunTimeline.Raw[] | null;
        created_at: string;
        modified_at: string;
    }
}
