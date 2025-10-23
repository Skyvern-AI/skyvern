import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { Workflow } from "../types/Workflow.js";
export declare const Response: core.serialization.Schema<serializers.getWorkflows.Response.Raw, Skyvern.Workflow[]>;
export declare namespace Response {
    type Raw = Workflow.Raw[];
}
