import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const CredentialTypeOutput: core.serialization.Schema<serializers.CredentialTypeOutput.Raw, Skyvern.CredentialTypeOutput>;
export declare namespace CredentialTypeOutput {
    type Raw = "password" | "credit_card";
}
