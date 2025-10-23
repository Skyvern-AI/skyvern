import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const DownloadToS3BlockYaml: core.serialization.ObjectSchema<serializers.DownloadToS3BlockYaml.Raw, Skyvern.DownloadToS3BlockYaml>;
export declare namespace DownloadToS3BlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url: string;
    }
}
