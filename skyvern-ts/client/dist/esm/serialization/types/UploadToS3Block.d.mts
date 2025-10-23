import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const UploadToS3Block: core.serialization.ObjectSchema<serializers.UploadToS3Block.Raw, Skyvern.UploadToS3Block>;
export declare namespace UploadToS3Block {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        path?: string | null;
    }
}
