import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const UploadToS3BlockYaml: core.serialization.ObjectSchema<serializers.UploadToS3BlockYaml.Raw, Skyvern.UploadToS3BlockYaml>;
export declare namespace UploadToS3BlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        path?: string | null;
    }
}
