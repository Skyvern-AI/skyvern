import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const DownloadToS3BlockYaml: core.serialization.ObjectSchema<serializers.DownloadToS3BlockYaml.Raw, Skyvern.DownloadToS3BlockYaml>;
export declare namespace DownloadToS3BlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url: string;
    }
}
