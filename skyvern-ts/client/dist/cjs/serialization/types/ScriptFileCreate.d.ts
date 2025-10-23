import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { FileEncoding } from "./FileEncoding.js";
export declare const ScriptFileCreate: core.serialization.ObjectSchema<serializers.ScriptFileCreate.Raw, Skyvern.ScriptFileCreate>;
export declare namespace ScriptFileCreate {
    interface Raw {
        path: string;
        content: string;
        encoding?: FileEncoding.Raw | null;
        mime_type?: string | null;
    }
}
