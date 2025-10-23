import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ThoughtScenario } from "./ThoughtScenario.js";
import { ThoughtType } from "./ThoughtType.js";
export declare const Thought: core.serialization.ObjectSchema<serializers.Thought.Raw, Skyvern.Thought>;
export declare namespace Thought {
    interface Raw {
        thought_id: string;
        task_id: string;
        organization_id: string;
        workflow_run_id?: string | null;
        workflow_run_block_id?: string | null;
        workflow_id?: string | null;
        workflow_permanent_id?: string | null;
        user_input?: string | null;
        observation?: string | null;
        thought?: string | null;
        answer?: string | null;
        thought_type?: ThoughtType.Raw | null;
        thought_scenario?: ThoughtScenario.Raw | null;
        output?: Record<string, unknown> | null;
        input_token_count?: number | null;
        output_token_count?: number | null;
        reasoning_token_count?: number | null;
        cached_token_count?: number | null;
        thought_cost?: number | null;
        created_at: string;
        modified_at: string;
    }
}
