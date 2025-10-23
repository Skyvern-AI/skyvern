import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const UploadToS3BlockYaml: core.serialization.ObjectSchema<serializers.UploadToS3BlockYaml.Raw, Skyvern.UploadToS3BlockYaml>;
export declare namespace UploadToS3BlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        path?: string | null;
    }
}
