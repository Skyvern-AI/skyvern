import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const DownloadToS3Block: core.serialization.ObjectSchema<serializers.DownloadToS3Block.Raw, Skyvern.DownloadToS3Block>;
export declare namespace DownloadToS3Block {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        url: string;
    }
}
