import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ArtifactType } from "./ArtifactType.js";
export declare const Artifact: core.serialization.ObjectSchema<serializers.Artifact.Raw, Skyvern.Artifact>;
export declare namespace Artifact {
    interface Raw {
        created_at: string;
        modified_at: string;
        artifact_id: string;
        artifact_type: ArtifactType.Raw;
        uri: string;
        task_id?: string | null;
        step_id?: string | null;
        workflow_run_id?: string | null;
        workflow_run_block_id?: string | null;
        observer_cruise_id?: string | null;
        observer_thought_id?: string | null;
        ai_suggestion_id?: string | null;
        signed_url?: string | null;
        organization_id: string;
    }
}
