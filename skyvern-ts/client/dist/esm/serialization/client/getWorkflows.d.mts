import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { Workflow } from "../types/Workflow.mjs";
export declare const Response: core.serialization.Schema<serializers.getWorkflows.Response.Raw, Skyvern.Workflow[]>;
export declare namespace Response {
    type Raw = Workflow.Raw[];
}
