import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { AwsSecretParameter } from "./AwsSecretParameter.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const SendEmailBlock: core.serialization.ObjectSchema<serializers.SendEmailBlock.Raw, Skyvern.SendEmailBlock>;
export declare namespace SendEmailBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        smtp_host: AwsSecretParameter.Raw;
        smtp_port: AwsSecretParameter.Raw;
        smtp_username: AwsSecretParameter.Raw;
        smtp_password: AwsSecretParameter.Raw;
        sender: string;
        recipients: string[];
        subject: string;
        body: string;
        file_attachments?: string[] | null;
    }
}
