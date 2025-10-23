import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { Thought } from "./Thought.js";
import { WorkflowRunBlock } from "./WorkflowRunBlock.js";
import { WorkflowRunTimelineType } from "./WorkflowRunTimelineType.js";
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
