import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { FileEncoding } from "./FileEncoding.mjs";
export declare const ScriptFileCreate: core.serialization.ObjectSchema<serializers.ScriptFileCreate.Raw, Skyvern.ScriptFileCreate>;
export declare namespace ScriptFileCreate {
    interface Raw {
        path: string;
        content: string;
        encoding?: FileEncoding.Raw | null;
        mime_type?: string | null;
    }
}
