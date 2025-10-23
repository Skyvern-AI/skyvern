import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { Artifact } from "../types/Artifact.mjs";
export declare const Response: core.serialization.Schema<serializers.getRunArtifacts.Response.Raw, Skyvern.Artifact[]>;
export declare namespace Response {
    type Raw = Artifact.Raw[];
}
