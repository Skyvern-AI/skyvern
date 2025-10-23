import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const SendEmailBlockYaml: core.serialization.ObjectSchema<serializers.SendEmailBlockYaml.Raw, Skyvern.SendEmailBlockYaml>;
export declare namespace SendEmailBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        smtp_host_secret_parameter_key: string;
        smtp_port_secret_parameter_key: string;
        smtp_username_secret_parameter_key: string;
        smtp_password_secret_parameter_key: string;
        sender: string;
        recipients: string[];
        subject: string;
        body: string;
        file_attachments?: string[] | null;
    }
}
