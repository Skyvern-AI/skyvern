import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { Artifact } from "../types/Artifact.js";
export declare const Response: core.serialization.Schema<serializers.getRunArtifacts.Response.Raw, Skyvern.Artifact[]>;
export declare namespace Response {
    type Raw = Artifact.Raw[];
}
